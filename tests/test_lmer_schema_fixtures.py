"""Shape contract for the shared plexus fixture trees (issue #353).

``tests/plexus_fixtures.py`` builds the two trees every lmer-schema subsystem
tests against — the validator (green/lenient tiers), the registry and form
detection, and the work CLI's dual-read — and those consumers use it READ-ONLY.
This module pins the shape they are entitled to rely on: a consumer that fails
against these trees has found a bug in the consumer, never a silently drifted
fixture. Every assertion here is a promise the builder makes to someone else.

The expectations come straight from the spec: the §2 layout, the §4 manifest
(D8 multi-remote entry, D19 repoless entry), the D17/D18 two-scope memory
under ``_agents/``, the D22 session-unit record under ``_runs/<slug>/``, and
the §9 legacy↔schema name mapping for the legacy twin.
"""

import json
import os
from pathlib import Path

import pytest
import yaml

from tests import plexus_fixtures as pf
from tests.plexus_fixtures import (
    PlexusTree,
    build_legacy_plexus,
    build_schema_plexus,
)

#: The bare legacy area names §9 renames; none may survive in schema form.
LEGACY_AREA_NAMES = frozenset({
    "runs", "specs", "memory", "info", "taskdef", "napkin", "agent-files",
    "mirrors", "knowledge", "plans", "archive",
})


def _dirs_under(root):
    """Every directory below *root*, as paths relative to it (symlinks are
    not followed, so a symlinked entry is reported once and never descended)."""
    out = []
    for current, dirnames, _files in os.walk(root, followlinks=False):
        for name in dirnames:
            out.append(Path(current, name).relative_to(root))
    return out


def _memory_type(path):
    """``metadata.type`` from a memory file's frontmatter (§3.4)."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    front = text.split("---\n", 2)[1]
    data = yaml.safe_load(front)
    return data["metadata"]["type"]


def _memory_index_targets(index):
    """Filenames each ``MEMORY.md`` line links to — one line per memory."""
    targets = []
    for line in index.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        assert line.startswith("- ["), f"unexpected MEMORY.md line: {line!r}"
        targets.append(line.split("](", 1)[1].split(")", 1)[0])
    return targets


# ---------------------------------------------------------------------------
# The builder API — identical for both forms, fresh tree per call
# ---------------------------------------------------------------------------


class TestBuilderApi:
    def test_fixtures_return_the_typed_map(self, schema_plexus, legacy_plexus):
        assert isinstance(schema_plexus, PlexusTree)
        assert isinstance(legacy_plexus, PlexusTree)
        assert schema_plexus.form == pf.SCHEMA_FORM
        assert legacy_plexus.form == pf.LEGACY_FORM

    def test_every_mapped_path_exists(self, schema_plexus, legacy_plexus):
        """The map is the consumer's address book; a None is a documented
        "this form has no such thing", anything else must be on disk."""
        for tree in (schema_plexus, legacy_plexus):
            for field, value in tree.paths().items():
                if value is None:
                    continue
                assert value.exists() or value.is_symlink(), f"{tree.form}.{field} -> {value} missing"
                assert value == tree.root or tree.root in value.parents, (
                    f"{tree.form}.{field} escapes the tree: {value}"
                )

    def test_each_call_builds_a_fresh_independent_tree(self, tmp_path):
        first = build_schema_plexus(tmp_path / "one")
        second = build_schema_plexus(tmp_path / "two")
        assert first.root != second.root
        (first.root / "_lmer.yaml").unlink()
        assert (second.root / "_lmer.yaml").is_file()

    def test_fixture_trees_live_under_tmp_path(self, schema_plexus, legacy_plexus, tmp_path):
        for tree in (schema_plexus, legacy_plexus):
            assert tmp_path in tree.root.parents

    def test_builder_refuses_the_operational_work_repo(self, tmp_path, monkeypatch):
        """The leak guard in conftest would catch a write under /work after
        the fact; the builder refuses before creating anything."""
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "work-mirror"))
        with pytest.raises(ValueError, match="operational work repo"):
            build_schema_plexus(tmp_path / "work-mirror" / "plexus")
        with pytest.raises(ValueError, match="operational work repo"):
            build_legacy_plexus(Path("/work") / "plexus")
        assert not (tmp_path / "work-mirror").exists()

    def test_builder_refuses_an_existing_non_empty_root(self, tmp_path):
        """A tree is built, never merged into something already there."""
        root = tmp_path / "busy"
        root.mkdir()
        (root / "stray.txt").write_text("x\n", encoding="utf-8")
        with pytest.raises(FileExistsError):
            build_schema_plexus(root)


# ---------------------------------------------------------------------------
# Schema form — the one form (§2)
# ---------------------------------------------------------------------------


class TestSchemaFormTree:
    def test_manifest_is_the_spec_shape(self, schema_plexus):
        manifest = schema_plexus.manifest
        assert manifest == schema_plexus.root / pf.MANIFEST_NAME
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert data["schema"] == 1 and not isinstance(data["schema"], bool)
        assert data["name"] == pf.INSTANCE_NAME
        assert set(data) == {"schema", "name", "projects", "sources", "housekeeping"}

    def test_registry_has_a_multi_remote_entry(self, schema_plexus):
        """D8: ``repo:`` is the primary, ``remotes:`` lists ADDITIONAL remotes
        (more than one, never repeating the primary)."""
        data = yaml.safe_load(schema_plexus.manifest.read_text(encoding="utf-8"))
        entry = data["projects"][pf.PROJECT]
        assert entry["repo"] == pf.PRIMARY_REPO == schema_plexus.primary_repo
        assert entry["remotes"] == list(pf.REMOTES) == list(schema_plexus.remotes)
        assert len(entry["remotes"]) >= 2
        assert pf.PRIMARY_REPO not in entry["remotes"]
        assert len(set(entry["remotes"])) == len(entry["remotes"])

    def test_registry_has_a_repoless_entry(self, schema_plexus):
        """D19: a project is a short name with a directory; a repo binding is
        optional. ``description:`` is the recommended human hint."""
        data = yaml.safe_load(schema_plexus.manifest.read_text(encoding="utf-8"))
        entry = data["projects"][pf.REPOLESS_PROJECT]
        assert "repo" not in entry and "remotes" not in entry
        assert entry["description"]
        assert schema_plexus.repoless_dir == schema_plexus.root / pf.REPOLESS_PROJECT
        assert schema_plexus.repoless_dir.is_dir()

    def test_registry_names_exactly_the_project_dirs(self, schema_plexus):
        data = yaml.safe_load(schema_plexus.manifest.read_text(encoding="utf-8"))
        assert set(data["projects"]) == {pf.PROJECT, pf.REPOLESS_PROJECT}
        for name in data["projects"]:
            assert (schema_plexus.root / name).is_dir()
            assert pf.SHORT_NAME_RE.fullmatch(name)

    def test_sources_stay_on_the_plexus_host(self, schema_plexus):
        """§4 same-host guard: a declared source on another host is an ERROR,
        so the fixture keeps every source on the primary remote's host."""
        data = yaml.safe_load(schema_plexus.manifest.read_text(encoding="utf-8"))
        host = pf.PRIMARY_REPO.split("/", 1)[0]
        assert data["sources"]
        for entry in data["sources"].values():
            assert f"://{host}/" in entry["repo"]

    def test_top_level_is_schema_namespace_plus_content(self, schema_plexus):
        """Everything at the root is either ``_``-owned, a registered short
        name, a dotted host bucket, or human content (a README)."""
        root = schema_plexus.root
        names = {p.name for p in root.iterdir()}
        expected_schema = {
            pf.MANIFEST_NAME, "_knowledge", "_napkin", "_taskdef", "_agents", "_archive",
        }
        assert expected_schema <= names
        others = names - expected_schema
        assert others == {pf.PROJECT, pf.REPOLESS_PROJECT, pf.BUCKET_HOST, "README.md"}
        assert "." in pf.BUCKET_HOST  # the dot is what marks a bucket (§1)

    def test_no_bare_legacy_names_anywhere(self, schema_plexus):
        """D6: once migrated, every schema-owned name carries the prefix."""
        bare = [
            rel for rel in _dirs_under(schema_plexus.root)
            if rel.name in LEGACY_AREA_NAMES
            and not any(part == "_agents" for part in rel.parts[:-1])
        ]
        assert bare == [], f"bare legacy names in schema tree: {bare}"
        assert not (schema_plexus.root / "sources.yaml").exists()

    def test_agents_surface_at_both_scopes(self, schema_plexus):
        """D18: ``_agents/`` at instance and project scope; ``claude`` and
        ``mirrors`` live at the instance, memory at both."""
        instance = schema_plexus.root / "_agents"
        project = schema_plexus.project_dir / "_agents"
        assert schema_plexus.agents_claude_dir == instance / "claude"
        assert schema_plexus.mirrors_dir == instance / "mirrors"
        assert (instance / "claude" / "skills").is_dir()
        assert (instance / "mirrors" / "masterplan" / "MIRROR.md").is_file()
        assert schema_plexus.instance_memory_dir == instance / "memory"
        assert schema_plexus.project_memory_dir == project / "memory"
        assert (instance / "memory").is_dir() and (project / "memory").is_dir()

    def test_one_memory_file_per_scope_routed_by_type(self, schema_plexus):
        """D17: instance scope holds a ``user`` memory, project scope holds a
        ``project`` memory; each scope's ``MEMORY.md`` indexes exactly its file."""
        scopes = {
            schema_plexus.instance_memory_dir: "user",
            schema_plexus.project_memory_dir: "project",
        }
        for scope_dir, expected_type in scopes.items():
            files = sorted(p for p in scope_dir.glob("*.md") if p.name != "MEMORY.md")
            assert len(files) == 1, f"{scope_dir} holds {files}"
            assert _memory_type(files[0]) == expected_type
            assert _memory_index_targets(scope_dir / "MEMORY.md") == [files[0].name]
        assert schema_plexus.memory_files == {
            "user": schema_plexus.instance_memory_dir / pf.USER_MEMORY_FILE,
            "project": schema_plexus.project_memory_dir / pf.PROJECT_MEMORY_FILE,
        }

    def test_project_areas_are_underscore_named(self, schema_plexus):
        project = schema_plexus.project_dir
        assert project == schema_plexus.root / pf.PROJECT
        assert {p.name for p in project.iterdir()} == {
            "_runs", "_specs", "_plans", "_agents", "_info", "_taskdef",
        }
        assert (project / "_plans" / "PLANS.md").is_file()
        assert list((project / "_info").glob("*.md"))

    def test_runs_slug_groups_two_independent_sessions(self, schema_plexus):
        """D22: ``_runs/<slug>/`` is a grouping level holding NOTHING but
        session dirs; each session dir is a complete record."""
        runs = schema_plexus.runs_dir
        assert runs == schema_plexus.project_dir / "_runs"
        assert [p.name for p in runs.iterdir()] == [pf.SLUG]
        group = runs / pf.SLUG
        children = sorted(group.iterdir())
        assert all(p.is_dir() for p in children), "nothing but session dirs at the grouping level"
        assert [p.name for p in children] == list(pf.SESSION_DIR_NAMES)
        assert tuple(children) == schema_plexus.session_dirs
        assert len(children) == 2

    def test_session_dir_names_are_session_ids_optionally_named(self, schema_plexus):
        first, second = schema_plexus.session_dirs
        assert first.name == pf.SESSION_IDS[0]
        assert second.name == f"{pf.SESSION_IDS[1]}--{pf.SESSION_NAME}"
        for sid in pf.SESSION_IDS:
            assert pf.SESSION_ID_RE.fullmatch(sid), sid
        # timestamp-first ids list launches in start order (§3.5)
        assert sorted(pf.SESSION_IDS) == list(pf.SESSION_IDS)

    def test_each_session_is_a_self_contained_record(self, schema_plexus):
        """State files per docs/RUN-STATE.md, shape unchanged — location only."""
        for session_dir, sid in zip(schema_plexus.session_dirs, pf.SESSION_IDS):
            state = yaml.safe_load((session_dir / "state.yaml").read_text(encoding="utf-8"))
            assert state["schema"] == 1
            assert state["slug"] == pf.SLUG
            assert state["status"] in {"in-progress", "complete", "archived"}
            events = [
                json.loads(line)
                for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            assert {e["session"] for e in events} == {sid}, "a session writes only its own dir"
            assert any(e["type"] == "session_start" for e in events)
            assert (session_dir / "log.yaml").is_file()

    def test_sessions_do_not_share_anything(self, schema_plexus):
        first, second = schema_plexus.session_dirs
        first_state = yaml.safe_load((first / "state.yaml").read_text(encoding="utf-8"))
        second_state = yaml.safe_load((second / "state.yaml").read_text(encoding="utf-8"))
        assert first_state["name"] is None
        assert second_state["name"] == pf.SESSION_NAME
        assert first_state["status"] != second_state["status"]
        assert first_state["target"] == second_state["target"]

    def test_specs_index_links_into_a_session_dir(self, schema_plexus):
        specs = schema_plexus.specs_dir
        assert specs == schema_plexus.project_dir / "_specs"
        (entry,) = schema_plexus.specs_entries
        assert entry.parent == specs
        assert entry.is_symlink()
        target = os.readlink(entry)
        assert not os.path.isabs(target)
        assert target.startswith(f"../_runs/{pf.SLUG}/{pf.SESSION_IDS[0]}/")
        assert entry.resolve() == (schema_plexus.session_dirs[0] / "spec.md").resolve()
        assert entry.resolve().is_file()

    def test_host_bucket_is_project_shaped_and_unregistered(self, schema_plexus):
        """D9: an unnamed repo's work sits at ``{host}/{project}/`` — same
        shape inside, no registry entry, no invented short name."""
        bucket = schema_plexus.bucket_dir
        assert bucket == schema_plexus.root / pf.BUCKET_HOST / pf.BUCKET_PROJECT
        assert (bucket / "_runs").is_dir()
        data = yaml.safe_load(schema_plexus.manifest.read_text(encoding="utf-8"))
        registered = set()
        for entry in data["projects"].values():
            registered.add(entry.get("repo"))
            registered.update(entry.get("remotes", []))
        assert f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}" not in registered
        # and no registered remote has a REAL dir at its {host}/{project}
        # path (§4 bucket ↔ registry collision is an ERROR)
        for remote in registered - {None}:
            assert not (schema_plexus.root / remote).exists()

    def test_legacy_only_fields_are_none(self, schema_plexus):
        assert schema_plexus.sources_file is None


# ---------------------------------------------------------------------------
# Legacy form — same shape, bare names, no manifest (§9)
# ---------------------------------------------------------------------------


class TestLegacyFormTree:
    def test_no_manifest_and_no_underscore_names(self, legacy_plexus):
        assert legacy_plexus.manifest is None
        assert not (legacy_plexus.root / pf.MANIFEST_NAME).exists()
        underscored = [
            rel for rel in _dirs_under(legacy_plexus.root)
            if any(part.startswith("_") for part in rel.parts)
        ]
        assert underscored == [], f"schema-owned names in legacy tree: {underscored}"

    def test_project_lives_at_host_project(self, legacy_plexus):
        host, path = pf.PRIMARY_REPO.split("/", 1)
        assert legacy_plexus.project_dir == legacy_plexus.root / host / path
        assert legacy_plexus.project_dir.is_dir()
        assert legacy_plexus.repoless_dir is None
        assert legacy_plexus.bucket_dir is None

    def test_top_level_mirrors_the_live_layout(self, legacy_plexus):
        names = {p.name for p in legacy_plexus.root.iterdir()}
        host = pf.PRIMARY_REPO.split("/", 1)[0]
        assert names == {
            host, "agent-files", "mirrors", "taskdef", "napkin", "sources.yaml", "README.md",
        }

    def test_bare_instance_surfaces(self, legacy_plexus):
        root = legacy_plexus.root
        assert legacy_plexus.agents_claude_dir == root / "agent-files" / "claude"
        assert (root / "agent-files" / "claude" / "skills").is_dir()
        assert legacy_plexus.mirrors_dir == root / "mirrors"
        assert (root / "mirrors" / "masterplan" / "MIRROR.md").is_file()
        assert legacy_plexus.instance_memory_dir is None  # no instance scope pre-D17
        assert legacy_plexus.knowledge_dir is None
        assert legacy_plexus.plans_dir is None
        assert legacy_plexus.archive_dir is None

    def test_standalone_sources_yaml(self, legacy_plexus):
        sources = legacy_plexus.sources_file
        assert sources == legacy_plexus.root / "sources.yaml"
        data = yaml.safe_load(sources.read_text(encoding="utf-8"))
        assert data["schema"] == 1
        assert set(data) == {"schema", "sources"}
        host = pf.PRIMARY_REPO.split("/", 1)[0]
        for entry in data["sources"].values():
            assert f"://{host}/" in entry["repo"]

    def test_project_areas_are_bare_named(self, legacy_plexus):
        project = legacy_plexus.project_dir
        assert {p.name for p in project.iterdir()} == {
            "runs", "specs", "memory", "info", "taskdef",
        }

    def test_both_memory_types_land_in_the_project_dir(self, legacy_plexus):
        """Pre-D17 every type lands in ``<project>/memory/`` — exactly what
        §9 step 3's lift-by-type migrates. Same two files as the schema tree."""
        memory = legacy_plexus.project_memory_dir
        assert memory == legacy_plexus.project_dir / "memory"
        files = sorted(p for p in memory.glob("*.md") if p.name != "MEMORY.md")
        assert [p.name for p in files] == sorted([pf.USER_MEMORY_FILE, pf.PROJECT_MEMORY_FILE])
        assert {_memory_type(p) for p in files} == {"user", "project"}
        assert sorted(_memory_index_targets(memory / "MEMORY.md")) == [p.name for p in files]
        assert legacy_plexus.memory_files == {
            "user": memory / pf.USER_MEMORY_FILE,
            "project": memory / pf.PROJECT_MEMORY_FILE,
        }

    def test_one_shared_run_dir_with_interleaved_sessions(self, legacy_plexus):
        """Legacy ``runs/<slug>/`` is ONE shared dir whose history interleaves
        several sessions (§3.5) — the very thing D22 replaces."""
        runs = legacy_plexus.runs_dir
        assert runs == legacy_plexus.project_dir / "runs"
        assert [p.name for p in runs.iterdir()] == [pf.SLUG]
        run_dir = runs / pf.SLUG
        assert legacy_plexus.session_dirs == (run_dir,)
        state = yaml.safe_load((run_dir / "state.yaml").read_text(encoding="utf-8"))
        assert state["schema"] == 1 and state["slug"] == pf.SLUG
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert {e["session"] for e in events} == set(pf.SESSION_IDS)
        assert sum(e["type"] == "session_start" for e in events) == 2
        assert (run_dir / "log.yaml").is_file()

    def test_specs_index_links_into_the_run_dir(self, legacy_plexus):
        specs = legacy_plexus.specs_dir
        assert specs == legacy_plexus.project_dir / "specs"
        (entry,) = legacy_plexus.specs_entries
        assert entry.is_symlink()
        assert os.readlink(entry) == f"../runs/{pf.SLUG}/spec.md"
        assert entry.resolve().is_file()


# ---------------------------------------------------------------------------
# The two trees are twins: same content, different names
# ---------------------------------------------------------------------------


class TestTwins:
    def test_same_identity_constants(self, schema_plexus, legacy_plexus):
        assert schema_plexus.primary_repo == legacy_plexus.primary_repo == pf.PRIMARY_REPO
        assert schema_plexus.slug == legacy_plexus.slug == pf.SLUG
        assert schema_plexus.session_ids == legacy_plexus.session_ids == pf.SESSION_IDS

    def test_same_memory_content(self, schema_plexus, legacy_plexus):
        for kind in ("user", "project"):
            assert (
                schema_plexus.memory_files[kind].read_text(encoding="utf-8")
                == legacy_plexus.memory_files[kind].read_text(encoding="utf-8")
            )

    def test_same_run_identity(self, schema_plexus, legacy_plexus):
        schema_state = yaml.safe_load(
            (schema_plexus.session_dirs[0] / "state.yaml").read_text(encoding="utf-8")
        )
        legacy_state = yaml.safe_load(
            (legacy_plexus.session_dirs[0] / "state.yaml").read_text(encoding="utf-8")
        )
        for key in ("schema", "slug", "taskdef", "target"):
            assert schema_state[key] == legacy_state[key]
