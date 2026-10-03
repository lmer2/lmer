"""``lmer_schema.checks.walk`` — the ONE project-directory walk (spec §1/§4,
D6/D9; SCHEMA.md "Concepts", "Ownership namespaces", "Host buckets"; issue
#353, !256 discussion 5).

``layout``, ``agents`` and ``index_links`` each grew their own descent and
gave DIFFERENT answers for the same tree; the runner then handed one list to
all three, so one module's reading silently became the rule for surfaces it
was never written for — and every module's own tests kept passing. That is
why the three divergence points are pinned HERE, on the tree the review
demonstrated them with:

    example.com/runs/                 (dotted top-level, itself project-shaped)
    example.com/sub/proj/_runs/
    other.com/_ns/proj/_runs/
    git.example.com/ns/proj/_runs/

    before:  layout       -> example.com            , git.example.com/ns/proj
             agents       -> example.com/sub/proj   , git.example.com/ns/proj ,
                             other.com/_ns/proj
             index_links  -> example.com            , git.example.com/ns/proj ,
                             other.com/_ns/proj

A module that later changes its mind about one of them fails in this file,
rather than narrowing someone else's surface in silence.
"""

from pathlib import Path

import pytest

from lmer_schema.checks import Form
from lmer_schema.checks import walk
from lmer_schema.checks.walk import (
    BUCKET,
    LEGACY_HOME,
    REGISTERED,
    UNREGISTERED,
    ProjectDir,
    find_project_dirs,
    project_dirs,
)

#: The review's tree, verbatim (module docstring).
DIVERGENCE_TREE = (
    "example.com/runs",
    "example.com/sub/proj/_runs",
    "other.com/_ns/proj/_runs",
    "git.example.com/ns/proj/_runs",
)

#: What the shared walk answers for it, once the three decisions are made.
DIVERGENCE_ANSWER = (
    "example.com",
    "git.example.com/ns/proj",
    "other.com/_ns/proj",
)


class _Registry(dict):
    """The registry surface this walk uses: iteration and ``in``, by name."""


def _tree(root, *paths):
    root.mkdir(parents=True, exist_ok=True)
    for path in paths:
        (root / path).mkdir(parents=True, exist_ok=True)
    return root


def _project_shaped(path, marker="_runs"):
    (path / marker).mkdir(parents=True, exist_ok=True)
    return path


def _rels(projects):
    return sorted(project.rel for project in projects)


@pytest.fixture
def divergence_tree(tmp_path):
    return _tree(tmp_path / "plexus", *DIVERGENCE_TREE)


# ---------------------------------------------------------------------------
# The three decisions (!256 discussion 5)
# ---------------------------------------------------------------------------


class TestTheThreeDecisions:
    def test_the_divergence_tree_has_one_answer(self, divergence_tree):
        assert _rels(project_dirs(divergence_tree)) == sorted(DIVERGENCE_ANSWER)

    def test_1_underscore_is_reserved_at_the_root_only(self, divergence_tree):
        """Decision 1: the ownership namespace (D6) is a rule about the names
        the SCHEMA owns — the instance root and the inside of a project. The
        components below a bucket's host dir come from the REMOTE, so a forge
        namespace is content: `other.com/_ns/proj/` is a project, and
        `layout`'s skip-at-every-level had stopped checking it."""
        assert "other.com/_ns/proj" in _rels(project_dirs(divergence_tree))

    def test_1_the_root_itself_still_reserves_underscore(self, divergence_tree):
        _project_shaped(divergence_tree / "_agents" / "memory")
        found = _rels(project_dirs(divergence_tree))
        assert not [rel for rel in found if rel.startswith("_")]

    def test_2_the_marker_test_comes_before_the_bucket_test(self, divergence_tree):
        """Decision 2: SCHEMA.md Concepts makes the marker test what separates
        a project from a namespace level, with no precondition about the host.
        A dotted top-level dir that is itself project-shaped IS that project,
        so the descent never starts and `example.com/sub/proj` is not a second
        one (`agents`' bucket-test-first reading found only the deeper dir)."""
        found = _rels(project_dirs(divergence_tree))
        assert "example.com" in found
        assert "example.com/sub/proj" not in found

    def test_2_a_dotted_project_still_carries_the_bucket_tier(self, divergence_tree):
        # It is reached through a dotted top-level name, so its contents are
        # checked at the legacy tier's severities (SCHEMA.md "Host buckets").
        found = {project.rel: project for project in project_dirs(divergence_tree)}
        assert found["example.com"].kind == BUCKET
        assert found["example.com"].form is Form.LEGACY

    def test_3_a_registered_dir_is_included_unshaped(self, divergence_tree):
        """Decision 3, first half: a registered project's directory is in
        scope whether or not it has grown areas yet."""
        (divergence_tree / "widgets").mkdir()
        found = {
            p.rel: p
            for p in project_dirs(divergence_tree, registry=_Registry(widgets=None))
        }
        assert found["widgets"].kind == REGISTERED
        assert found["widgets"].name == "widgets"

    def test_3_a_registered_entry_without_a_directory_is_not_invented(
        self, divergence_tree
    ):
        registry = _Registry(widgets=None)
        assert "widgets" not in _rels(project_dirs(divergence_tree, registry=registry))

    def test_3_a_collided_bucket_is_never_suppressed_here(self, divergence_tree):
        """Decision 3, second half: `tree.bucket-collision` is `layout`'s own
        REPORTING concern. On a split tree that directory is the one most
        worth checking, so the shared list keeps it — the suppression must not
        remove a directory from another module's checks."""
        found = _rels(project_dirs(divergence_tree, registry=_Registry(proj=None)))
        assert "git.example.com/ns/proj" in found

    def test_a_registered_name_is_listed_once(self, divergence_tree):
        _project_shaped(divergence_tree / "widgets")
        found = _rels(project_dirs(divergence_tree, registry=_Registry(widgets=None)))
        assert found.count("widgets") == 1


# ---------------------------------------------------------------------------
# The descent: the marker test, never depth
# ---------------------------------------------------------------------------


class TestDescent:
    def test_the_top_itself_can_be_the_project(self, tmp_path):
        top = _project_shaped(tmp_path / "top")
        assert find_project_dirs(top) == (top,)

    def test_a_missing_path_is_empty(self, tmp_path):
        assert find_project_dirs(tmp_path / "nope") == ()

    def test_results_are_sorted(self, tmp_path):
        top = tmp_path / "host"
        for name in ("zeta", "alpha", "mid"):
            _project_shaped(top / name)
        assert [p.name for p in find_project_dirs(top)] == ["alpha", "mid", "zeta"]

    @pytest.mark.parametrize(
        "deep",
        ["widgets", "acme/widgets", "acme/team/widgets", "a/b/c/d/e", "a/b/c/d/e/f"],
    )
    def test_found_at_any_depth(self, tmp_path, deep):
        # SCHEMA.md Concepts and Host buckets: "not path depth". A cap would
        # lose the deepest subgroup trees, which is what D9 exists to prevent.
        top = tmp_path / "git.example.org"
        target = _project_shaped(top.joinpath(*deep.split("/")))
        assert find_project_dirs(top) == (target,)

    def test_nothing_below_a_project_is_a_second_project(self, tmp_path):
        top = tmp_path / "git.example.org"
        project = _project_shaped(top / "acme" / "widgets")
        _project_shaped(project / "vendor" / "inner")
        assert find_project_dirs(top) == (project,)

    def test_a_legacy_shaped_dir_counts(self, tmp_path):
        top = tmp_path / "git.example.org"
        old = _project_shaped(top / "acme" / "old", marker="runs")
        assert find_project_dirs(top) == (old,)

    def test_symlinked_levels_are_not_followed(self, tmp_path):
        # A §9 compatibility link points at a project already found;
        # following one would report it twice (and loop).
        real = _project_shaped(tmp_path / "real")
        (tmp_path / "host").mkdir()
        (tmp_path / "host" / "link").symlink_to(real)
        assert find_project_dirs(tmp_path / "host") == ()

    def test_hidden_levels_are_never_descended(self, tmp_path):
        top = tmp_path / "git.example.org"
        _project_shaped(top / ".git" / "modules" / "x")
        assert find_project_dirs(top) == ()

    def test_the_walk_has_no_depth_rule(self):
        source = Path(walk.__file__).read_text(encoding="utf-8")
        assert "MAX_DEPTH" not in source
        assert not any(name.endswith("DEPTH") for name in dir(walk))


# ---------------------------------------------------------------------------
# What each project carries
# ---------------------------------------------------------------------------


class TestProjectDir:
    def test_a_bucket_is_unnamed_and_legacy_tier(self, divergence_tree):
        found = {p.rel: p for p in project_dirs(divergence_tree, form=Form.SCHEMA)}
        bucket = found["git.example.com/ns/proj"]
        assert bucket.kind == BUCKET
        assert bucket.name is None
        assert bucket.form is Form.LEGACY  # SCHEMA.md "Host buckets"

    def test_in_a_legacy_tree_the_same_path_is_the_project_s_only_home(
        self, divergence_tree
    ):
        found = {p.rel: p for p in project_dirs(divergence_tree, form=Form.LEGACY)}
        assert found["git.example.com/ns/proj"].kind == LEGACY_HOME

    def test_everything_else_carries_the_tree_s_form(self, tmp_path):
        root = _tree(tmp_path / "plexus")
        _project_shaped(root / "widgets")
        found = {p.rel: p for p in project_dirs(root, form=Form.SCHEMA)}
        assert found["widgets"].kind == UNREGISTERED
        assert found["widgets"].form is Form.SCHEMA

    def test_no_form_means_the_report_s_own(self, tmp_path):
        root = _tree(tmp_path / "plexus")
        _project_shaped(root / "widgets")
        assert project_dirs(root)[0].form is None

    def test_it_is_os_pathlike_for_the_check_modules(self, divergence_tree):
        # The per-project modules take a caller-supplied list and do
        # `Path(entry)`; the runner walks the tree once, here.
        projects = project_dirs(divergence_tree)
        assert [Path(p) for p in projects] == [p.path for p in projects]

    def test_it_is_a_frozen_value(self, divergence_tree):
        project = project_dirs(divergence_tree)[0]
        assert isinstance(project, ProjectDir)
        with pytest.raises((AttributeError, TypeError)):
            project.kind = BUCKET


# ---------------------------------------------------------------------------
# The top level
# ---------------------------------------------------------------------------


class TestTopLevel:
    def test_a_dotless_dir_holding_projects_is_descended(self, tmp_path):
        # §1: a dotless host is not supported as a bucket, but the projects
        # below it are what `layout`'s "unregistered project or dotless host
        # bucket" warning names.
        root = _tree(tmp_path / "plexus")
        _project_shaped(root / "localhost" / "acme" / "widgets")
        assert _rels(project_dirs(root)) == ["localhost/acme/widgets"]

    def test_a_dotless_project_is_not_a_bucket_tier(self, tmp_path):
        root = _tree(tmp_path / "plexus")
        _project_shaped(root / "localhost" / "acme" / "widgets")
        assert project_dirs(root, form=Form.SCHEMA)[0].form is Form.SCHEMA

    def test_a_dir_that_is_not_project_shaped_is_content(self, tmp_path):
        root = _tree(tmp_path / "plexus", "notes/drafts")
        (root / "notes" / "thought.md").write_text("hi\n", encoding="utf-8")
        assert project_dirs(root) == ()

    def test_a_top_level_file_is_never_a_project(self, tmp_path):
        root = _tree(tmp_path / "plexus")
        (root / "README.md").write_text("hi\n", encoding="utf-8")
        assert project_dirs(root) == ()

    def test_a_top_level_symlink_is_never_a_project(self, tmp_path):
        root = _tree(tmp_path / "plexus")
        real = _project_shaped(root / "widgets")
        (root / "compat").symlink_to(real)
        assert _rels(project_dirs(root)) == ["widgets"]

    def test_bare_instance_surfaces_are_not_projects(self, tmp_path):
        # The legacy twins of the `_` areas: instance surfaces, reported by
        # the modules that own them, never a project (SCHEMA.md "Legacy form").
        root = _tree(tmp_path / "plexus")
        _project_shaped(root / "agent-files", marker="memory")
        assert project_dirs(root) == ()

    def test_a_missing_root_is_empty(self, tmp_path):
        assert project_dirs(tmp_path / "nope") == ()

    def test_registered_dirs_come_first_then_the_top_level_in_name_order(
        self, divergence_tree
    ):
        _project_shaped(divergence_tree / "widgets")
        found = project_dirs(divergence_tree, registry=_Registry(widgets=None))
        assert [p.rel for p in found] == [
            "widgets",
            "example.com",
            "git.example.com/ns/proj",
            "other.com/_ns/proj",
        ]


class TestGrouping:
    def test_projects_group_by_their_top_level_name(self, divergence_tree):
        grouped = walk.by_top_level(project_dirs(divergence_tree))
        assert set(grouped) == {"example.com", "git.example.com", "other.com"}
        assert [p.rel for p in grouped["other.com"]] == ["other.com/_ns/proj"]

    def test_two_projects_under_one_host(self, tmp_path):
        root = _tree(tmp_path / "plexus")
        _project_shaped(root / "git.example.org" / "acme" / "one")
        _project_shaped(root / "git.example.org" / "acme" / "two")
        grouped = walk.by_top_level(project_dirs(root))
        assert [p.rel for p in grouped["git.example.org"]] == [
            "git.example.org/acme/one",
            "git.example.org/acme/two",
        ]
