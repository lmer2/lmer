"""The run-state kernel's PATH layer, routed by form (issue #353, D20/D22).

`work_repo.run_state` used to spell the run area twice: `runs_base()` hung
`runs` off the env trio's base, and `run_rel_for_dir_name()` re-built
`{host}/{project}/runs/<dir>` from `LMER_REPO_HOST`/`LMER_REPO_PROJECT` by
hand. In a schema-form tree both are wrong — the area is `_runs/` and the
project dir is whatever the registry resolved, which for a named project is
not a `{host}/{project}` path at all — and a hand-built relative path would
stage a directory nothing wrote.

Three behaviours here, one class each:

- **Legacy is byte-identical.** `_reference_runs_base` and
  `_reference_run_rel_for_dir_name` ARE the pre-routing implementations,
  copied verbatim, and the matrix compares against them rather than against
  a hand-written expectation.
- **Both paths come off the ONE resolved base.** `runs_base()` routes
  through `project_area("runs", "_runs")` and the relative path is that base
  made relative to the work-repo root, so a registry-resolved short name
  stages `lmer/_runs/<dir>` and a host bucket stages
  `{host}/{project}/_runs/<dir>` (SCHEMA.md, "Host buckets": a bucket in a
  schema tree carries the `_` areas exactly as a named project does).
- **The content resolvers are base-agnostic.** `find_run_dir`,
  `find_successor_run_dir`, `slug_available` and `_unreadable_state_candidate`
  match on recorded state under whatever base they are handed, with the
  archive/dot-dir skips intact, in either form.

Path layer only: the D22 session dir (`_runs/<slug>/<session-id>/`) is the
next task's, so nothing here pins where inside `_runs/` a record is written.
"""

import os
from pathlib import Path

import pytest
import yaml

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    BUCKET_HOST,
    BUCKET_PROJECT,
    PRIMARY_REPO,
    PROJECT,
    build_legacy_plexus,
    build_schema_plexus,
)
from work_repo import run_state, utils
from work_repo.utils import work_repo_root


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, and no announcement carried between tests."""
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()


def _set_context(monkeypatch, root, *, host=None, project=None, url=None):
    """Point the resolver at *root* with the launch context a session has."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    for name, value in (
        ("LMER_REPO_HOST", host),
        ("LMER_REPO_PROJECT", project),
        ("LMER_REPO_URL", url),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _set_tree_context(monkeypatch, tree):
    """The launch context for the fixture tree's registered project."""
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(monkeypatch, tree.root, host=host, project=path)


def _write_run(rdir, slug, **fields):
    """A minimal run record under *rdir* — enough for the content resolvers."""
    state = {"schema": 1, "slug": slug, "name": None, "status": "in-progress"}
    state.update(fields)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / run_state.STATE_FILE).write_text(yaml.safe_dump(state), encoding="utf-8")
    return rdir


# ---------------------------------------------------------------------------
# Legacy: byte-identical
# ---------------------------------------------------------------------------


def _reference_runs_base():
    """`runs_base()` BEFORE form routing (run_state.py:156 at the time), with
    the pre-dual-read `_work_repo_base()` inlined so the oracle depends on
    nothing this task touched."""
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    if not repo_host or not repo_project:
        return None
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    return work_repo_path / repo_host / repo_project / "runs"


def _reference_run_rel_for_dir_name(dir_name):
    """`run_rel_for_dir_name()` verbatim (run_state.py:395-408 at the time)."""
    host = os.environ.get("LMER_REPO_HOST")
    project = os.environ.get("LMER_REPO_PROJECT")
    if not host or not project:
        return None
    return f"{host}/{project}/runs/{dir_name}"


#: The same contexts the root resolver's oracle uses (issue #353 T19) — the
#: "no run context" pairs and the awkward spellings a launch really produces.
_LEGACY_CONTEXTS = [
    (None, None),
    ("git.example.com", None),
    (None, "group/proj"),
    ("", "group/proj"),
    ("git.example.com", ""),
    ("git.example.com", "group/proj"),
    ("git.example.com", "group/sub/proj"),
    ("git.example.com:8443", "group/proj"),
    ("localhost", "proj"),
]

#: Dir names the staging path has to keep spelling identically: the bare
#: slug, the name-bearing form a freeze leaves, and a re-slugged address.
_DIR_NAMES = [
    "develop-issue-123",
    "release-repo--declined",
    "release-repo-v0.6.0",
]


class TestLegacyByteIdentical:
    """A manifest-less tree builds exactly the paths it did before routing."""

    @pytest.mark.parametrize("host,project", _LEGACY_CONTEXTS)
    def test_runs_base_matches_the_pre_routing_implementation(
        self, monkeypatch, tmp_path, host, project
    ):
        _set_context(monkeypatch, tmp_path, host=host, project=project)
        assert run_state.runs_base() == _reference_runs_base()

    @pytest.mark.parametrize("host,project", _LEGACY_CONTEXTS)
    @pytest.mark.parametrize("dir_name", _DIR_NAMES)
    def test_run_rel_matches_the_pre_routing_implementation(
        self, monkeypatch, tmp_path, host, project, dir_name
    ):
        _set_context(monkeypatch, tmp_path, host=host, project=project)
        assert run_state.run_rel_for_dir_name(dir_name) == \
            _reference_run_rel_for_dir_name(dir_name)

    def test_the_legacy_fixture_tree_resolves_at_its_bare_names(
        self, monkeypatch, tmp_path
    ):
        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        _set_tree_context(monkeypatch, tree)
        host, path = PRIMARY_REPO.split("/", 1)
        assert run_state.runs_base() == tree.runs_dir
        assert run_state.runs_base() == tree.project_dir / "runs"
        assert run_state.run_rel_for_dir_name(tree.slug) == \
            f"{host}/{path}/runs/{tree.slug}"

    def test_the_env_trio_still_needs_both_halves(self, monkeypatch, tmp_path):
        _set_context(monkeypatch, tmp_path, host="git.example.com")
        assert run_state.runs_base() is None
        assert run_state.run_rel_for_dir_name("anything") is None
        assert run_state.run_rel_path_candidates() == []

    def test_work_repo_path_still_defaults_to_work(self, monkeypatch):
        """The default root is the resolver's, not a second copy here."""
        monkeypatch.delenv("LMER_WORK_REPO_PATH", raising=False)
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
        assert run_state.runs_base() == _reference_runs_base()
        assert run_state.run_rel_for_dir_name("develop-issue-1") == \
            "git.example.com/group/proj/runs/develop-issue-1"


# ---------------------------------------------------------------------------
# Schema form: one resolved base, both paths off it
# ---------------------------------------------------------------------------


@pytest.fixture
def schema_tree(tmp_path):
    return build_schema_plexus(tmp_path / "plexus-schema")


@pytest.fixture
def legacy_tree(tmp_path):
    return build_legacy_plexus(tmp_path / "plexus-legacy")


class TestRunsBaseRoutesByForm:
    """`<project>/runs` legacy, `<project>/_runs` schema — off the resolved
    project dir, never the env trio."""

    def test_registry_hit_resolves_the_short_name_tree(self, monkeypatch, schema_tree):
        _set_tree_context(monkeypatch, schema_tree)
        assert run_state.runs_base() == schema_tree.runs_dir
        assert run_state.runs_base() == schema_tree.root / PROJECT / "_runs"

    def test_it_is_the_area_helper_the_root_resolver_owns(
        self, monkeypatch, schema_tree
    ):
        """One mapping, in one module: `runs_base()` IS `project_area()`."""
        _set_tree_context(monkeypatch, schema_tree)
        assert run_state.runs_base() == utils.project_area("runs", "_runs")

    def test_an_unregistered_repo_gets_the_bucket_s_schema_areas(
        self, monkeypatch, schema_tree
    ):
        """D9 + SCHEMA.md "Host buckets": the form of the TREE decides the
        names, so a bucket in a schema tree carries `_runs/` too."""
        _set_context(
            monkeypatch, schema_tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        assert run_state.runs_base() == schema_tree.bucket_dir / "_runs"

    def test_no_run_context_resolves_nothing(self, monkeypatch, schema_tree):
        _set_context(monkeypatch, schema_tree.root)
        assert run_state.runs_base() is None
        assert run_state.run_dir("develop-issue-1") is None

    def test_a_broken_manifest_refuses_rather_than_demoting(
        self, monkeypatch, schema_tree, capsys
    ):
        """D20: no fallback to `{host}/{project}/runs/` — that is the split
        tree the validator flags, written one record at a time."""
        (schema_tree.root / "_plexus.yaml").write_text(
            "schema: 1\nname: example\nprojects: [oops\n", encoding="utf-8"
        )
        _set_tree_context(monkeypatch, schema_tree)
        host, path = PRIMARY_REPO.split("/", 1)
        assert run_state.runs_base() is None
        assert run_state.run_rel_for_dir_name("develop-issue-1") is None
        assert run_state.run_rel_path_candidates() == []
        assert str(schema_tree.manifest) in capsys.readouterr().err

    def test_the_creation_address_follows_the_base(self, monkeypatch, schema_tree):
        """`run_dir()`'s fallback address hangs off the routed base: `<base>/<slug>`
        in a legacy tree, and this launch's SESSION dir under it in schema form.

        Updated by T21: when this file was written the session level did not
        exist yet and the docstring said "nothing here pins where inside
        `_runs/` a record is written" — but the assertion did pin it, at the
        grouping level D22 says is never written to.
        """
        _set_tree_context(monkeypatch, schema_tree)
        monkeypatch.setenv("LMER_SESSION_ID", "20260824-101500-1-11111")
        assert run_state.run_dir("develop-issue-404") == (
            schema_tree.runs_dir / "develop-issue-404" / "20260824-101500-1-11111"
        )


class TestRunRelPathsBuildFromTheResolvedBase:
    """The work-repo-relative staging path is the resolved base made relative
    to the root — never a re-built `{host}/{project}/runs/<dir>`."""

    def test_a_registry_hit_stages_the_short_name_path(
        self, monkeypatch, schema_tree
    ):
        _set_tree_context(monkeypatch, schema_tree)
        host, path = PRIMARY_REPO.split("/", 1)
        rel = run_state.run_rel_for_dir_name("develop-issue-123")
        assert rel == f"{PROJECT}/_runs/develop-issue-123"
        assert rel != f"{host}/{path}/runs/develop-issue-123"

    def test_a_bucket_stages_the_bucket_path(self, monkeypatch, schema_tree):
        _set_context(
            monkeypatch, schema_tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        assert run_state.run_rel_for_dir_name("develop-issue-123") == \
            f"{BUCKET_HOST}/{BUCKET_PROJECT}/_runs/develop-issue-123"

    @pytest.mark.parametrize("form", ["schema", "legacy"])
    @pytest.mark.parametrize("dir_name", _DIR_NAMES)
    def test_the_relative_path_addresses_the_dir_it_names(
        self, monkeypatch, schema_tree, legacy_tree, form, dir_name
    ):
        """The invariant that makes the string worth staging: root + rel is
        the directory `runs_base()` resolves, in either form."""
        tree = schema_tree if form == "schema" else legacy_tree
        _set_tree_context(monkeypatch, tree)
        rel = run_state.run_rel_for_dir_name(dir_name)
        assert rel is not None
        assert work_repo_root() / rel == run_state.runs_base() / dir_name

    def test_candidates_are_the_same_construction(self, monkeypatch, schema_tree):
        """One owner for the shape: the candidates are the resolved dir and
        the bare slug run through `run_rel_for_dir_name`."""
        _set_tree_context(monkeypatch, schema_tree)
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "issue-77")
        _write_run(schema_tree.runs_dir / "develop-issue-77--named", "develop-issue-77")
        assert run_state.run_rel_path_candidates() == [
            f"{PROJECT}/_runs/develop-issue-77--named",
            f"{PROJECT}/_runs/develop-issue-77",
        ]
        assert run_state.run_rel_path() == f"{PROJECT}/_runs/develop-issue-77--named"

    def test_an_unresolved_run_stages_the_bare_address(self, monkeypatch, schema_tree):
        _set_tree_context(monkeypatch, schema_tree)
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "issue-78")
        assert run_state.run_rel_path_candidates() == [
            f"{PROJECT}/_runs/develop-issue-78",
        ]


# ---------------------------------------------------------------------------
# The content resolvers: whatever base they are handed
# ---------------------------------------------------------------------------


class TestContentResolversAreBaseAgnostic:
    """Resolution is by recorded `state.slug`, under the base of the day —
    the form changes where the base is, never how a run is found."""

    @pytest.fixture(params=["schema", "legacy"])
    def tree(self, request, schema_tree, legacy_tree):
        return schema_tree if request.param == "schema" else legacy_tree

    def test_find_run_dir_resolves_under_the_routed_base(self, monkeypatch, tree):
        _set_tree_context(monkeypatch, tree)
        base = run_state.runs_base()
        _write_run(base / "develop-issue-42--renamed", "develop-issue-42",
                   name="renamed")
        assert run_state.find_run_dir("develop-issue-42") == \
            base / "develop-issue-42--renamed"
        assert run_state.find_run_dir("renamed", match_names=True) == \
            base / "develop-issue-42--renamed"

    def test_the_archive_and_dot_dir_skips_survive(self, monkeypatch, tree):
        """`.new-*` orphans and the archive subtree share the namespace and
        are never matched (RUN-STATE.md §6)."""
        _set_tree_context(monkeypatch, tree)
        base = run_state.runs_base()
        _write_run(base / f"{run_state.TMP_DIR_PREFIX}s-1-abc", "develop-issue-43")
        _write_run(base / run_state.ARCHIVE_DIR, "develop-issue-44")
        assert run_state.find_run_dir("develop-issue-43") is None
        assert run_state.find_run_dir("develop-issue-44") is None

    def test_successor_resolution_survives(self, monkeypatch, tree):
        _set_tree_context(monkeypatch, tree)
        base = run_state.runs_base()
        _write_run(base / "release-repo-v0.6.0", "release-repo-v0.6.0",
                   reslugged_from=["release-repo"])
        assert run_state.find_successor_run_dir("release-repo") == \
            base / "release-repo-v0.6.0"
        assert run_state.resolve_run_dir("release-repo") == \
            base / "release-repo-v0.6.0"

    def test_slug_availability_survives(self, monkeypatch, tree):
        _set_tree_context(monkeypatch, tree)
        base = run_state.runs_base()
        assert run_state.slug_available("develop-issue-45")
        _write_run(base / "develop-issue-45", "develop-issue-45")
        assert not run_state.slug_available("develop-issue-45")
        assert run_state.slug_available(
            "develop-issue-45", exclude=base / "develop-issue-45"
        )

    def test_the_unreadable_state_candidate_survives(self, monkeypatch, tree):
        _set_tree_context(monkeypatch, tree)
        base = run_state.runs_base()
        stranded = base / "develop-issue-46--stranded"
        stranded.mkdir(parents=True)
        (stranded / run_state.STATE_FILE).write_text("{[bad", encoding="utf-8")
        assert run_state._unreadable_state_candidate(base, "develop-issue-46") == \
            stranded
        assert run_state.run_dir("develop-issue-46") == stranded

    def test_an_explicit_base_still_wins(self, monkeypatch, tree, tmp_path):
        """Callers that hand a base in (the platform's mirror walk) are
        unaffected by the form of the session's own tree."""
        _set_tree_context(monkeypatch, tree)
        elsewhere = tmp_path / "elsewhere"
        _write_run(elsewhere / "develop-issue-47", "develop-issue-47")
        assert run_state.find_run_dir("develop-issue-47") is None
        assert run_state.find_run_dir("develop-issue-47", base=elsewhere) == \
            elsewhere / "develop-issue-47"
