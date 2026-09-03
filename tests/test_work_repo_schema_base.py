"""Dual-read in the work CLI's single root resolver (issue #353, spec D20).

`work_repo.utils` is the ONE module that knows the second set of names. Every
sibling reaches the form and the project dir through `work_repo_layout()` (and
the area helpers built on it), so a second module can never re-derive
`_plexus.yaml`, `_info/` or `_agents/memory/` and drift from this one — the
source guard at the bottom of this file pins that.

Three behaviours are load-bearing and each has its own class here:

- **Legacy is byte-identical.** A manifest-less tree resolves exactly as the
  pre-dual-read resolver did; `_legacy_reference_base` IS that resolver,
  copied verbatim, and the matrix compares against it rather than against a
  hand-written expectation.
- **Schema form resolves by registry, falls back to the bucket.** Registry
  first, `{host}/{project}` host bucket second, and NEVER a derived short
  name (D9).
- **The failure modes split three ways (D20).** A core-API IMPORT failure
  against a tree with NOTHING schema-shaped in it (an old checkout) degrades
  to legacy and never raises — hook-facing always-exit-0 commands sit on this
  resolver. Anything that happens with a PRESENT manifest never degrades to
  legacy, because falling back would write this session's record into
  `{host}/{project}/runs/` inside a schema-form tree — the silent demotion
  D20 forbids and the split tree the validator then flags ERROR. A PARSE
  failure of that manifest resolves no base at all and names the manifest; a
  registry RULE violation resolves the `{host}/{project}` host bucket in
  SCHEMA form (D9's holding place, not a demotion) because only the identity
  layer is unusable — the same reading the validator's runner gives the same
  input (its rule 3).
"""

import os
import sys
from pathlib import Path

import pytest

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    BUCKET_HOST,
    BUCKET_PROJECT,
    PRIMARY_REPO,
    PROJECT,
    REMOTES,
    REPOLESS_PROJECT,
    build_legacy_plexus,
    build_schema_plexus,
)
from work_repo import utils
from work_repo.utils import (
    LEGACY_FORM,
    SCHEMA_FORM,
    _work_repo_base,
    instance_memory_dir,
    project_area,
    project_info_dir,
    project_memory_dir,
    task_info_dir,
    task_target_dir,
    work_repo_form,
    work_repo_layout,
    work_repo_manifest_problem,
    work_repo_root,
)


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


def _break_manifest(root, text):
    """Replace the tree's manifest with *text* (a present, unusable one)."""
    (Path(root) / "_plexus.yaml").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Legacy: byte-identical
# ---------------------------------------------------------------------------


def _legacy_reference_base():
    """The resolver BEFORE dual-read, verbatim (utils.py:228 at the time).

    Kept as the oracle rather than as prose so "legacy is byte-identical" is
    checked, not claimed: any divergence in a manifest-less tree fails here.
    """
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    if not repo_host or not repo_project:
        return None
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    return work_repo_path / repo_host / repo_project


#: (host, project) pairs the resolver has to keep answering identically —
#: including the ones that are "no run context" and the awkward spellings a
#: launch really produces (a subgroup path, a host with a port).
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


class TestLegacyByteIdentical:
    """A manifest-less tree resolves exactly as it did before dual-read."""

    @pytest.mark.parametrize("host,project", _LEGACY_CONTEXTS)
    def test_base_matches_the_pre_dual_read_resolver(
        self, monkeypatch, tmp_path, host, project
    ):
        _set_context(monkeypatch, tmp_path, host=host, project=project)
        assert _work_repo_base() == _legacy_reference_base()

    @pytest.mark.parametrize("host,project", _LEGACY_CONTEXTS)
    def test_helpers_match_the_pre_dual_read_paths(
        self, monkeypatch, tmp_path, host, project
    ):
        _set_context(monkeypatch, tmp_path, host=host, project=project)
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "issue-353")
        base = _legacy_reference_base()
        if base is None:
            assert project_info_dir() is None
            assert project_memory_dir() is None
            assert task_info_dir() is None
            assert task_target_dir() is None
            return
        assert project_info_dir() == base / "info"
        assert project_memory_dir() == base / "memory"
        assert task_info_dir() == base / "develop" / "info"
        assert task_target_dir() == base / "develop" / "issue-353"

    def test_work_repo_path_still_defaults_to_work(self, monkeypatch):
        monkeypatch.delenv("LMER_WORK_REPO_PATH", raising=False)
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
        assert work_repo_root() == Path("/work")
        assert _work_repo_base() == Path("/work/git.example.com/group/proj")

    def test_repo_url_alone_is_still_no_run_context(self, monkeypatch, tmp_path):
        """Identity is the PATH in a legacy tree: a repo URL resolves nothing."""
        _set_context(monkeypatch, tmp_path, url=f"https://{PRIMARY_REPO}")
        assert _work_repo_base() is None
        assert work_repo_form() == LEGACY_FORM

    def test_legacy_fixture_tree(self, monkeypatch, tmp_path):
        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, tree.root, host=host, project=path)
        layout = work_repo_layout()
        assert layout.form == LEGACY_FORM
        assert layout.manifest is None
        assert layout.problem is None
        assert layout.project is None
        assert layout.base == tree.project_dir
        assert project_info_dir() == tree.info_dir
        assert project_memory_dir() == tree.project_memory_dir
        assert project_area("runs", "_runs") == tree.runs_dir
        assert project_area("specs", "_specs") == tree.specs_dir

    def test_no_instance_memory_scope_in_a_legacy_tree(self, monkeypatch, tmp_path):
        """D17's second scope arrives with the form: legacy has one memory dir."""
        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, tree.root, host=host, project=path)
        assert tree.instance_memory_dir is None
        assert instance_memory_dir() is None

    def test_a_manifest_below_the_root_does_not_change_the_form(
        self, monkeypatch, tmp_path
    ):
        """Detection is at the work-repo ROOT (D20), not anywhere in the tree."""
        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        (tree.project_dir / "_plexus.yaml").write_text(
            "schema: 1\nname: nested\n", encoding="utf-8"
        )
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, tree.root, host=host, project=path)
        assert work_repo_form() == LEGACY_FORM
        assert _work_repo_base() == tree.project_dir


# ---------------------------------------------------------------------------
# Schema form: registry first, bucket second, name never derived
# ---------------------------------------------------------------------------


@pytest.fixture
def schema_tree(tmp_path):
    return build_schema_plexus(tmp_path / "plexus-schema")


class TestSchemaFormResolution:
    """`_plexus.yaml` present -> the registry decides the project dir."""

    def test_form_and_manifest(self, monkeypatch, schema_tree):
        _set_context(monkeypatch, schema_tree.root)
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.manifest == schema_tree.manifest
        assert layout.problem is None

    def test_registry_hit_on_the_composed_host_project_url(
        self, monkeypatch, schema_tree
    ):
        """No LMER_REPO_URL: the launch URL is https://{host}/{project}."""
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, schema_tree.root, host=host, project=path)
        layout = work_repo_layout()
        assert layout.base == schema_tree.project_dir
        assert layout.base == schema_tree.root / PROJECT
        assert layout.project == PROJECT

    def test_registry_hit_on_an_additional_remote(self, monkeypatch, schema_tree):
        """D8: repo: and every remotes: entry resolve to the ONE tree."""
        _set_context(
            monkeypatch,
            schema_tree.root,
            host="gitlab.example.com",
            project="group/project",
            url=f"https://{REMOTES[0]}",
        )
        assert _work_repo_base() == schema_tree.project_dir

    @pytest.mark.parametrize(
        "spelling",
        [
            "https://{repo}",
            "https://{repo}.git",
            "git@{host}:{path}.git",
            "ssh://git@{host}/{path}",
            "https://oauth2:glpat-xxxxxxxxxxxxxxxxxxxxxxxx@{repo}.git",
        ],
    )
    def test_every_spelling_of_the_remote_resolves(
        self, monkeypatch, schema_tree, spelling
    ):
        """Normalization is the core's (D21) — no second copy here."""
        host, path = PRIMARY_REPO.split("/", 1)
        url = spelling.format(repo=PRIMARY_REPO, host=host, path=path)
        _set_context(monkeypatch, schema_tree.root, host=host, project=path, url=url)
        assert _work_repo_base() == schema_tree.project_dir

    def test_area_helpers_route_to_the_schema_names(self, monkeypatch, schema_tree):
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, schema_tree.root, host=host, project=path)
        assert project_info_dir() == schema_tree.info_dir
        assert project_info_dir() == schema_tree.project_dir / "_info"
        assert project_memory_dir() == schema_tree.project_memory_dir
        assert project_memory_dir() == schema_tree.project_dir / "_agents" / "memory"
        assert instance_memory_dir() == schema_tree.instance_memory_dir
        assert instance_memory_dir() == schema_tree.root / "_agents" / "memory"
        assert project_area("runs", "_runs") == schema_tree.runs_dir
        assert project_area("specs", "_specs") == schema_tree.specs_dir

    def test_task_dirs_are_unchanged_in_schema_form(self, monkeypatch, schema_tree):
        """The `{task_type}/{target}/` pair has no schema name (Appendix A):
        it stays a read-side fallback hanging off whatever base resolved."""
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, schema_tree.root, host=host, project=path)
        base = schema_tree.project_dir
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "issue-353")
        assert task_info_dir() == base / "develop" / "info"
        assert task_target_dir() == base / "develop" / "issue-353"

    def test_bucket_hit_for_an_unregistered_repo(self, monkeypatch, schema_tree):
        """D9: no entry -> the existing {host}/{project} path, unnamed."""
        _set_context(
            monkeypatch,
            schema_tree.root,
            host=BUCKET_HOST,
            project=BUCKET_PROJECT,
        )
        layout = work_repo_layout()
        assert layout.base == schema_tree.bucket_dir
        assert layout.project is None

    def test_bucket_for_a_repo_with_nothing_on_disk_yet(
        self, monkeypatch, schema_tree
    ):
        """A first launch has no bucket dir yet; the work still lands there
        rather than under an invented short name (D9)."""
        _set_context(
            monkeypatch,
            schema_tree.root,
            host="gitlab.example.com",
            project="unnamed/brand-new",
        )
        layout = work_repo_layout()
        assert layout.base == schema_tree.root / "gitlab.example.com" / "unnamed" / "brand-new"
        assert not layout.base.exists()
        assert layout.project is None

    def test_a_name_is_never_derived(self, monkeypatch, schema_tree):
        """The last path segment is NOT taken as a short name."""
        _set_context(
            monkeypatch,
            schema_tree.root,
            host="gitlab.example.com",
            project="unnamed/widget",
        )
        assert _work_repo_base() != schema_tree.root / "widget"
        assert work_repo_layout().project is None

    def test_a_repoless_entry_never_matches(self, monkeypatch, schema_tree):
        """D19: a project with no repo answers to no URL."""
        _set_context(
            monkeypatch,
            schema_tree.root,
            host="gitlab.example.com",
            project="unnamed/widget",
        )
        assert _work_repo_base() != schema_tree.root / REPOLESS_PROJECT

    def test_dotless_host_falls_back_to_the_host_project_path(
        self, monkeypatch, schema_tree
    ):
        """No bucket can hold a dotless host (§1); the work still lands at
        the path lmer already writes rather than nowhere."""
        _set_context(monkeypatch, schema_tree.root, host="localhost", project="proj")
        assert _work_repo_base() == schema_tree.root / "localhost" / "proj"

    def test_no_run_context_still_resolves_no_base(self, monkeypatch, schema_tree):
        """Schema form does not invent a project out of an empty context."""
        _set_context(monkeypatch, schema_tree.root)
        assert _work_repo_base() is None
        assert project_info_dir() is None
        assert project_memory_dir() is None
        # The instance scope needs no run context — it hangs off the root.
        assert instance_memory_dir() == schema_tree.instance_memory_dir


# ---------------------------------------------------------------------------
# D20 failure mode 1: a PRESENT manifest that cannot be parsed
# ---------------------------------------------------------------------------


#: One case per way a manifest can be present and unusable.
_BROKEN_MANIFESTS = {
    "unparseable": "schema: 1\nname: example\nprojects: [oops\n",
    "not-a-mapping": "- a\n- b\n",
    "unsupported-schema": "schema: 99\nname: example\n",
    "schema-is-a-bool": "schema: true\nname: example\n",
    "projects-not-a-mapping": "schema: 1\nname: example\nprojects: [lmer]\n",
    "bad-registry-key": "schema: 1\nname: example\nprojects:\n  ../escape: {}\n",
}


class TestPresentButBrokenManifestFailsClosed:
    """D20: a present manifest is NEVER read as a legacy tree."""

    @pytest.fixture(autouse=True)
    def _broken_context(self, monkeypatch, schema_tree):
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, schema_tree.root, host=host, project=path)
        self.tree = schema_tree
        self.legacy_path = schema_tree.root / host / path

    @pytest.mark.parametrize("case", sorted(_BROKEN_MANIFESTS))
    def test_no_base_and_no_legacy_demotion(self, case, capsys):
        _break_manifest(self.tree.root, _BROKEN_MANIFESTS[case])
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.base is None
        assert layout.base != self.legacy_path
        assert layout.project is None

    @pytest.mark.parametrize("case", sorted(_BROKEN_MANIFESTS))
    def test_the_problem_names_the_manifest(self, case, capsys):
        _break_manifest(self.tree.root, _BROKEN_MANIFESTS[case])
        problem = work_repo_manifest_problem()
        assert problem is not None
        assert str(self.tree.manifest) in problem
        assert work_repo_layout().problem == problem

    @pytest.mark.parametrize("case", sorted(_BROKEN_MANIFESTS))
    def test_it_is_reported_not_swallowed(self, case, capsys):
        _break_manifest(self.tree.root, _BROKEN_MANIFESTS[case])
        assert _work_repo_base() is None
        err = capsys.readouterr().err
        assert str(self.tree.manifest) in err
        assert "_plexus.yaml" in err

    def test_reported_once_per_process(self, capsys):
        _break_manifest(self.tree.root, _BROKEN_MANIFESTS["unparseable"])
        _work_repo_base()
        assert str(self.tree.manifest) in capsys.readouterr().err
        _work_repo_base()
        project_info_dir()
        assert capsys.readouterr().err == ""

    def test_every_area_helper_refuses(self, capsys):
        """Nothing downstream gets a path to write into — the mutating verbs
        refuse and the read-only ones report and carry on."""
        _break_manifest(self.tree.root, _BROKEN_MANIFESTS["unparseable"])
        assert project_info_dir() is None
        assert project_memory_dir() is None
        assert instance_memory_dir() is None
        assert task_info_dir() is None
        assert task_target_dir() is None
        assert project_area("runs", "_runs") is None

    def test_a_manifest_that_is_a_directory(self, capsys):
        (self.tree.manifest).unlink()
        (self.tree.manifest).mkdir()
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.base is None
        assert str(self.tree.manifest) in (layout.problem or "")

    def test_a_dangling_manifest_symlink(self, capsys):
        (self.tree.manifest).unlink()
        (self.tree.manifest).symlink_to("nowhere.yaml")
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.base is None
        assert str(self.tree.manifest) in (layout.problem or "")

    def test_a_usable_manifest_reports_no_problem(self, capsys):
        assert work_repo_manifest_problem() is None
        assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# D20 failure mode 2: the core API is not importable
# ---------------------------------------------------------------------------


@pytest.fixture
def no_core_api(monkeypatch):
    """Make the core API unimportable, as an old checkout would."""
    for name in ("lmer_schema", "lmer_schema.form", "lmer_schema.registry",
                 "lmer_schema.manifest"):
        monkeypatch.setitem(sys.modules, name, None)


class TestCoreImportFailureWithNoManifestDegradesToLegacy:
    """An lmer with no `lmer_schema`, pointed at a tree with nothing
    schema-shaped at its root, reads it as legacy — silently, because
    hook-facing commands that always exit 0 sit on this resolver.

    This is the premise the degrade clause was written for: an old checkout
    against an unmigrated tree. Nothing is present that the missing core
    would have read differently, so there is nothing to demote.
    """

    @pytest.fixture(autouse=True)
    def _legacy_context(self, monkeypatch, tmp_path):
        self.tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        self.host, self.path = PRIMARY_REPO.split("/", 1)
        _set_context(
            monkeypatch, self.tree.root, host=self.host, project=self.path
        )

    def test_a_legacy_tree_degrades_without_raising(self, no_core_api, capsys):
        layout = work_repo_layout()
        assert layout.form == LEGACY_FORM
        assert layout.base == self.tree.project_dir
        assert layout.problem is None
        assert project_info_dir() == self.tree.info_dir
        assert instance_memory_dir() is None

    def test_it_is_silent(self, no_core_api, capsys):
        _work_repo_base()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_a_manifest_below_the_root_is_not_schema_shaped(
        self, no_core_api, capsys
    ):
        """Presence is asked at the work-repo ROOT with or without the core."""
        (self.tree.project_dir / "_plexus.yaml").write_text(
            "schema: 1\nname: nested\n", encoding="utf-8"
        )
        assert work_repo_form() == LEGACY_FORM
        assert _work_repo_base() == self.tree.project_dir
        assert capsys.readouterr().err == ""

    def test_no_context_is_still_none(self, monkeypatch, no_core_api):
        _set_context(monkeypatch, self.tree.root)
        assert _work_repo_base() is None


class TestCoreImportFailureWithAManifestFailsClosed:
    """A PRESENT manifest is never read as a legacy tree — not even when the
    core that would have read it cannot be imported.

    `except ImportError` cannot tell "there is no core" from "the core is
    here and broken" (a partial install, a broken transitive import such as
    PyYAML, a defect landed by a later wave), and only the first of those has
    nothing schema-shaped present — which is the premise the degrade clause
    was written for. Asking whether the manifest is there needs no core at
    all, and this module is the one place allowed to name it, so the two
    cases split: absent degrades exactly as before, present is a `problem`,
    reported exactly as a parse failure is and still never raised.
    """

    @pytest.fixture(autouse=True)
    def _schema_context(self, monkeypatch, schema_tree):
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, schema_tree.root, host=host, project=path)
        self.tree = schema_tree
        self.legacy_path = schema_tree.root / host / path

    def test_the_same_tree_resolves_normally_with_the_core(self):
        """The other half of the reproduction: nothing about the tree, the
        manifest or the launch context differs between these two tests."""
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.base == self.tree.project_dir
        assert layout.problem is None

    def test_a_valid_manifest_does_not_degrade(self, no_core_api, capsys):
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.base is None
        assert layout.base != self.legacy_path
        assert layout.project is None
        assert str(self.tree.manifest) in (layout.problem or "")

    def test_it_reports_rather_than_raises(self, no_core_api, capsys):
        assert _work_repo_base() is None
        err = capsys.readouterr().err
        assert str(self.tree.manifest) in err
        assert "_plexus.yaml" in err

    def test_reported_once_per_process(self, no_core_api, capsys):
        _work_repo_base()
        assert str(self.tree.manifest) in capsys.readouterr().err
        _work_repo_base()
        project_info_dir()
        assert capsys.readouterr().err == ""

    def test_every_area_helper_refuses(self, no_core_api, capsys):
        assert project_info_dir() is None
        assert project_memory_dir() is None
        assert instance_memory_dir() is None
        assert task_info_dir() is None
        assert task_target_dir() is None
        assert project_area("runs", "_runs") is None

    def test_a_broken_manifest_fails_closed_too(self, no_core_api, capsys):
        """With no core there is no telling a valid manifest from a broken
        one — and no need to: presence is the whole question."""
        _break_manifest(self.tree.root, _BROKEN_MANIFESTS["unparseable"])
        layout = work_repo_layout()
        assert layout.base is None
        assert layout.base != self.legacy_path
        assert work_repo_manifest_problem() is not None

    def test_a_dangling_manifest_symlink_is_present(self, no_core_api, capsys):
        """Someone put a manifest there; `exists()` follows the link and says
        no, so presence is asked exactly as the core asks it."""
        self.tree.manifest.unlink()
        self.tree.manifest.symlink_to("nowhere.yaml")
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.base is None
        assert str(self.tree.manifest) in (layout.problem or "")


# ---------------------------------------------------------------------------
# D20 failure mode 3: the manifest parses, its REGISTRY does not
# ---------------------------------------------------------------------------


#: One case per way a well-formed manifest can break a registry RULE — a
#: `RegistryError`, which subclasses `ManifestError` but is not a parse
#: failure: the document loaded, only the identity layer is unusable.
_BROKEN_REGISTRIES = {
    "remotes-without-repo": (
        "schema: 1\nname: example\nprojects:\n  widgets:\n"
        "    remotes:\n      - gitlab.example.com/other/thing\n"
    ),
    "one-remote-two-entries": (
        "schema: 1\nname: example\nprojects:\n"
        "  widgets:\n    repo: gitlab.example.com/a/b\n"
        "  gadgets:\n    repo: https://gitlab.example.com/a/b.git\n"
    ),
    "repo-repeated-in-its-own-remotes": (
        "schema: 1\nname: example\nprojects:\n  widgets:\n"
        "    repo: gitlab.example.com/a/b\n"
        "    remotes:\n      - git@gitlab.example.com:a/b.git\n"
    ),
    "description-not-a-string": (
        "schema: 1\nname: example\nprojects:\n  widgets:\n"
        "    description: 5\n"
    ),
}


class TestBrokenRegistryKeepsTheHostBucket:
    """A registry RULE violation is an identity-layer failure, not a parse
    failure — the same reading the validator's runner gives the same input
    (runner rule 3: "the manifest parsed, so the tree, its form and every
    non-registry surface still say something true; only the identity layer
    is unusable").

    So the base resolves to the `{host}/{project}` host bucket in SCHEMA form
    — D9's documented holding place, and no demotion, so D20 holds — and the
    `problem` is recorded and announced exactly as a parse failure's is. The
    alternative refuses a base for EVERY session against the work repo,
    including one whose repo is unregistered and would never have consulted
    the registry; D9 says work is never refused and never lost, and a session
    that records into the bucket while an entry exists leaves a split tree
    the validator flags ERROR — visible and recoverable, which is the better
    failure than recording nothing at all.
    """

    @pytest.fixture(autouse=True)
    def _tree(self, schema_tree):
        self.tree = schema_tree

    @pytest.mark.parametrize("case", sorted(_BROKEN_REGISTRIES))
    def test_an_unregistered_session_keeps_its_bucket(
        self, monkeypatch, case, capsys
    ):
        """The reviewer's reproduction: this session never needed the
        registry — its repo is not in it — and one bad entry elsewhere must
        not take it down."""
        _break_manifest(self.tree.root, _BROKEN_REGISTRIES[case])
        _set_context(
            monkeypatch, self.tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        layout = work_repo_layout()
        assert layout.form == SCHEMA_FORM
        assert layout.base == self.tree.bucket_dir
        assert layout.project is None
        assert str(self.tree.manifest) in (layout.problem or "")

    @pytest.mark.parametrize("case", sorted(_BROKEN_REGISTRIES))
    def test_a_registered_session_falls_back_to_its_bucket(
        self, monkeypatch, case, capsys
    ):
        """A session that WOULD have matched cannot know it did — no name is
        derived and no short-name dir is used; its work parks in the bucket
        (D9), where a later `work adopt` can move it."""
        host, path = PRIMARY_REPO.split("/", 1)
        _break_manifest(self.tree.root, _BROKEN_REGISTRIES[case])
        _set_context(monkeypatch, self.tree.root, host=host, project=path)
        layout = work_repo_layout()
        assert layout.base == self.tree.root / host / path
        assert layout.base != self.tree.project_dir
        assert layout.project is None

    def test_the_bucket_is_written_in_the_schema_form(self, monkeypatch, capsys):
        """The point of resolving a base here rather than none: it is NOT a
        legacy demotion. The areas keep their schema names (D20)."""
        _break_manifest(
            self.tree.root, _BROKEN_REGISTRIES["remotes-without-repo"]
        )
        _set_context(
            monkeypatch, self.tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        bucket = self.tree.bucket_dir
        assert work_repo_form() == SCHEMA_FORM
        assert project_area("runs", "_runs") == bucket / "_runs"
        assert project_area("specs", "_specs") == bucket / "_specs"
        assert project_info_dir() == bucket / "_info"
        assert project_memory_dir() == bucket / "_agents" / "memory"

    def test_the_instance_memory_scope_still_resolves(self, monkeypatch, capsys):
        """It hangs off the root and asks the registry nothing (D17)."""
        _break_manifest(
            self.tree.root, _BROKEN_REGISTRIES["remotes-without-repo"]
        )
        _set_context(
            monkeypatch, self.tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        assert instance_memory_dir() == self.tree.instance_memory_dir

    def test_the_problem_is_reported_once(self, monkeypatch, capsys):
        _break_manifest(
            self.tree.root, _BROKEN_REGISTRIES["remotes-without-repo"]
        )
        _set_context(
            monkeypatch, self.tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        problem = work_repo_manifest_problem()
        assert problem is not None
        err = capsys.readouterr().err
        assert str(self.tree.manifest) in err
        _work_repo_base()
        project_info_dir()
        assert capsys.readouterr().err == ""

    def test_a_dotless_host_still_falls_back_to_the_host_project_path(
        self, monkeypatch, capsys
    ):
        """No bucket can hold a dotless host (§1); the work lands where lmer
        already writes it rather than nowhere."""
        _break_manifest(
            self.tree.root, _BROKEN_REGISTRIES["remotes-without-repo"]
        )
        _set_context(monkeypatch, self.tree.root, host="localhost", project="proj")
        assert _work_repo_base() == self.tree.root / "localhost" / "proj"

    def test_no_run_context_still_resolves_no_base(self, monkeypatch, capsys):
        """Nothing to resolve is still nothing to resolve — and not a base
        invented out of an empty context."""
        _break_manifest(
            self.tree.root, _BROKEN_REGISTRIES["remotes-without-repo"]
        )
        _set_context(monkeypatch, self.tree.root)
        layout = work_repo_layout()
        assert layout.base is None
        assert layout.problem is not None

    def test_a_parse_failure_is_still_fail_closed(self, monkeypatch, capsys):
        """The contrast, on one tree and one session: a document that does
        not load leaves the identity layer unreadable at a more basic level,
        so nothing resolves."""
        _set_context(
            monkeypatch, self.tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        _break_manifest(self.tree.root, _BROKEN_REGISTRIES["remotes-without-repo"])
        assert _work_repo_base() == self.tree.bucket_dir
        _break_manifest(self.tree.root, _BROKEN_MANIFESTS["unparseable"])
        assert _work_repo_base() is None


# ---------------------------------------------------------------------------
# Caching: none, deliberately
# ---------------------------------------------------------------------------


class TestNothingIsCachedAcrossCalls:
    """The resolver reads the env and the manifest on every call: the env is
    mutated at runtime (a masterplan sub-launch sets LMER_REPO_HOST/PROJECT)
    and the manifest is written by adoption while a container is up, so a
    per-process cache would answer with the tree as it USED to be."""

    def test_a_manifest_written_later_is_seen(self, monkeypatch, tmp_path):
        root = tmp_path / "plexus"
        tree = build_legacy_plexus(root)
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, root, host=host, project=path)
        assert work_repo_form() == LEGACY_FORM
        assert _work_repo_base() == tree.project_dir
        (root / "_plexus.yaml").write_text(
            f"schema: 1\nname: example\nprojects:\n  {PROJECT}:\n"
            f"    repo: {PRIMARY_REPO}\n",
            encoding="utf-8",
        )
        assert work_repo_form() == SCHEMA_FORM
        assert _work_repo_base() == root / PROJECT

    def test_a_changed_launch_context_re_resolves(self, monkeypatch, schema_tree):
        host, path = PRIMARY_REPO.split("/", 1)
        _set_context(monkeypatch, schema_tree.root, host=host, project=path)
        assert _work_repo_base() == schema_tree.project_dir
        monkeypatch.setenv("LMER_REPO_HOST", BUCKET_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", BUCKET_PROJECT)
        assert _work_repo_base() == schema_tree.bucket_dir


# ---------------------------------------------------------------------------
# One module knows the second set of names
# ---------------------------------------------------------------------------


class TestOnlyThisModuleKnowsTheSecondNames:
    """The defect this task exists to prevent is a sibling re-deriving the
    form. `work_repo.utils` owns the manifest name, the core import and the
    legacy<->schema area mapping; siblings route through `work_repo_layout()`
    / `project_area()`."""

    def _sibling_sources(self):
        package = Path(utils.__file__).parent
        return [
            path
            for path in sorted(package.glob("*.py"))
            if path.name != "utils.py"
        ]

    #: Naming the manifest, or importing the core API, IS re-deriving the
    #: form. Prose mentioning `lmer_schema` is fine — the import spellings
    #: are what a second implementation needs.
    @pytest.mark.parametrize(
        "token", ["_plexus.yaml", "import lmer_schema", "from lmer_schema"]
    )
    def test_no_sibling_re_derives_the_form(self, token):
        offenders = [
            path.name
            for path in self._sibling_sources()
            if token in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            f"{offenders} name {token!r} themselves; route through "
            "work_repo.utils.work_repo_layout() instead — exactly one module "
            "knows the second set of names (issue #353 D20)."
        )
