"""Container start places this launch's record by FORM (issue #353 D20/D22).

``clone_and_exec.ensure_work_repo_directory()`` runs once at container start,
before any ``work`` verb, and until now it only knew one layout: the legacy
``{host}/{project}`` + ``{host}/{project}/{task_type}/{task_target}`` +
``info/`` chain. In a schema-form work repo none of those names is right —
the project dir is whatever the registry resolved (a short name, or a host
bucket for a repo nobody has named yet) and one launch owns one complete
session dir, ``<project>/_runs/<slug>/<session-id>/`` (D22).

What is pinned here, in the order the module reaches it:

- **Form detection costs one stat in a legacy tree.** The entrypoint runs
  standalone and must not depend on the package being importable (see its
  module docstring), so a tree with no ``_plexus.yaml`` answers "legacy"
  without importing anything — proved by resolving one with the core made
  unimportable. A tree that HAS a manifest and a build that cannot read it
  refuses to place anything rather than falling back to the legacy names
  (D20: that fallback is the split tree the validator flags ERROR).
- **Identity has ONE definition.** Registry lookup and remote normalization
  come from ``lmer_schema.registry`` through a lazy guarded import, not from
  a mirrored copy (D8/D21), and ``TestAgreesWithTheWorkCli`` pins the answer
  against ``work_repo.utils.work_repo_layout()`` and
  ``work_repo.run_state.run_dir()`` — the dir the entrypoint creates is the
  dir the work CLI later resolves, or the launch writes its record somewhere
  nothing reads.
- **Legacy is byte-for-byte.** The matrix compares the created tree against
  ``_reference_ensure_work_repo_directory``, the pre-routing implementation
  copied verbatim, rather than against a hand-written expectation.
- **A resume opens no second directory.** A resumed launch arrives with the
  resumed record's id (the host CLI's ``--session`` -> ``LMER_SESSION_ID``),
  and that record may be a NAMED one living at ``<session-id>--<name>/``, so
  the address is prepared only when the group holds no record for the id
  under either name — the rule ``run_state._named_session`` matches by,
  mirrored here and pinned against it in ``TestAgreesWithTheWorkCli``. The
  fresh-launch half is unchanged: an EMPTY dir, which is what lets ``work
  seed``'s rename replace it.
- **The minted session id can BE a directory name.** ``mint_session_id`` is
  where ``LMER_SESSION_ID`` comes from and in schema form that value names a
  directory, so its spelling is pinned against the work CLI's own guard
  (``run_state._usable_session_id`` / ``SESSION_ID_RE``) — a test that fails
  if either side drifts. ``main()`` mints before it places, since a session
  dir cannot be named by a variable that does not exist yet.
- **No work item, no suffix.** The group is keyed by ``_derive_run_slug``,
  and a target that is not a work item — nothing, a path, a bare repository
  URL, a document URL — derives the bare taskdef (run-identity spec §3), so
  a plain chat lands in ``_runs/chat/`` whatever the host exported and two
  such launches are two records in ONE group (#311). The rule mirrors
  ``run_state.derive_slug``; the §9.1 derive-slug table runs through both
  copies in ``TestAgreesWithTheWorkCli``. A work item — an issue, an MR, a
  branch, a SHA, a thread permalink the handler owns — keeps today's slug,
  and two launches on one work item are two records that never touch
  (#117, ``TestTwoLaunchesOnOneTarget``).
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.container import clone_and_exec
from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    BUCKET_HOST,
    BUCKET_PROJECT,
    PRIMARY_REPO,
    SESSION_DIR_NAMES,
    SESSION_IDS,
    SLUG,
    TARGET,
    TASKDEF,
    build_legacy_plexus,
    build_schema_plexus,
)
from lmer_platform import workrepo
from work_repo import run_state, utils

#: The entrypoint as the container runs it: a file, executed by path.
ENTRYPOINT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "lmer_cli" / "container" / "clone_and_exec.py"
)

#: A launch id in the shape mint_session_id produces.
SESSION_ID = "20260825-120000-4242-31245"

#: The launch context of the fixture tree's REGISTERED project, as the host
#: CLI exports it (``_parse_host_project`` splits the clone URL into these two).
PRIMARY_HOST, PRIMARY_PATH = PRIMARY_REPO.split("/", 1)

#: The §9.1 derive-slug table, bare half: every target that is NOT a work
#: item — nothing, a path, a bare repository URL (#81's ``.git`` spelling
#: included), a document URL on or off a forge — derives the bare taskdef
#: (spec §3). A lagging host still exports ``.`` or the raw clone URL; the
#: container classifies what it is given, so those land here too.
BARE_TARGETS = [
    pytest.param(None, id="no-target"),
    pytest.param("", id="empty-target"),
    pytest.param(".", id="dot"),
    pytest.param("./x", id="relative-path"),
    pytest.param("/abs", id="absolute-path"),
    pytest.param("https://h/g/p", id="bare-url"),
    pytest.param("https://h/g/p.git", id="bare-url-dot-git"),
    pytest.param("https://h/g/p/-/blob/main/x.md", id="document-url"),
    pytest.param(f"https://{PRIMARY_REPO}.git", id="issue-81-clone-url"),
    pytest.param("https://docs.python.org/3/library/os.html", id="off-forge-document"),
]

#: The table's work-item half: ``(taskdef, target, slug)``, the slug being
#: what each pair derived BEFORE the §3 rule — pinned so the rule changes
#: nothing for a work item. The thread permalinks are ``special`` (§2): the
#: handler owns them, and they keep their slug beside the work items.
WORK_ITEM_SLUGS = [
    pytest.param("develop", TARGET, SLUG, id="issue-url"),
    pytest.param(
        "review", f"https://{PRIMARY_REPO}/-/merge_requests/7", "review-mr-7", id="mr-url"
    ),
    pytest.param("review", "https://github.com/owner/repo/pull/9", "review-pr-9", id="pr-url"),
    pytest.param("review", "feature/some-branch", "review-feature-some-branch", id="branch"),
    pytest.param("develop", "a" * 40, "develop-" + "a" * 12, id="full-sha"),
    pytest.param(
        "chat",
        "https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456",
        "chat-p1700000000123456",
        id="slack-permalink",
    ),
    pytest.param(
        "chat",
        "https://matrix.to/#/!abc:example.org/$evt?via=example.org",
        "chat-$evt",
        id="matrix-permalink",
    ),
]

#: Both halves as ``(taskdef, target)`` — what the parity test walks.
SLUG_TABLE = [
    *(pytest.param("chat", *row.values, id=row.id) for row in BARE_TARGETS),
    *(pytest.param(*row.values[:2], id=row.id) for row in WORK_ITEM_SLUGS),
]


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, and no announcement carried between tests."""
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()


def _set_context(
    monkeypatch,
    root,
    *,
    host=None,
    project=None,
    url=None,
    session=SESSION_ID,
    task=TASKDEF,
    target=TARGET,
):
    """Point both subsystems at *root* with the env a launch carries."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    for name, value in (
        ("LMER_REPO_HOST", host),
        ("LMER_REPO_PROJECT", project),
        ("LMER_REPO_URL", url),
        ("LMER_SESSION_ID", session),
        ("LMER_TASK", task),
        ("LMER_TASK_TARGET", target),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _without_core():
    """Patch context making every ``lmer_schema`` import raise ImportError —
    a build that ships without the core, or with it half-installed.

    Each submodule the resolver names is nulled too: ``from x.y import z``
    consults ``sys.modules["x.y"]`` first, so nulling the package alone would
    leave the import working in any run that already imported it (and the
    guard passing for the wrong reason).
    """
    return patch.dict(
        sys.modules,
        {
            "lmer_schema": None,
            "lmer_schema.form": None,
            "lmer_schema.manifest": None,
            "lmer_schema.registry": None,
        },
    )


def _broken_manifest(root):
    """A root whose ``_plexus.yaml`` is present and cannot be parsed."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "_plexus.yaml").write_text("schema: 1\nprojects: [oops\n", encoding="utf-8")
    return root


def _dirs_in(group):
    """The directory names one group holds — what an operator sees in `ls`."""
    return sorted(entry.name for entry in group.iterdir() if entry.is_dir())


def _tree_dirs(root):
    """Every directory under *root*, relative and sorted — the shape a
    creation step left behind."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_dir()
    )


# The pre-routing implementation of ensure_work_repo_directory, copied
# VERBATIM (issue #353 predecessor). The legacy matrix compares against this
# rather than a hand-written list of paths, so "byte-for-byte" is measured
# against the code that shipped, not against a reading of it.
def _reference_ensure_work_repo_directory(
    work_repo_path, host, project, task_type, task_target
):
    if not host or not project:
        return

    project_dir_parts = [host, project]
    project_dir = work_repo_path
    for part in project_dir_parts:
        project_dir = project_dir / part
        project_dir.mkdir(parents=True, exist_ok=True)

    project_info_dir = project_dir / "info"
    project_info_dir.mkdir(parents=True, exist_ok=True)

    safe_task_type = task_type if task_type else "default"
    safe_task_target = (
        clone_and_exec.sanitize_task_target(task_target) if task_target else "default"
    )

    task_type_dir_parts = [host, project, safe_task_type]
    task_type_dir = work_repo_path
    for part in task_type_dir_parts:
        task_type_dir = task_type_dir / part
        task_type_dir.mkdir(parents=True, exist_ok=True)

    task_info_dir = task_type_dir / "info"
    task_info_dir.mkdir(parents=True, exist_ok=True)

    dir_parts = [host, project, safe_task_type, safe_task_target]

    target_dir = work_repo_path
    for part in dir_parts:
        target_dir = target_dir / part
        target_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Which form is this tree, and where does the work go
# ---------------------------------------------------------------------------


class TestPlacement:
    """_resolve_work_repo_placement: the form, the base, and the refusals."""

    def test_no_manifest_is_legacy_at_the_env_trio_path(self, tmp_path, monkeypatch):
        tree = build_legacy_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        placement = clone_and_exec._resolve_work_repo_placement(
            tree.root, PRIMARY_HOST, PRIMARY_PATH
        )
        assert placement.schema is False
        assert placement.base == tree.root / PRIMARY_HOST / PRIMARY_PATH
        assert placement.problem is None

    def test_a_legacy_tree_needs_no_core_at_all(self, tmp_path, monkeypatch):
        """The presence probe answers first, so the entrypoint stays
        stdlib-only on the path every unmigrated tree takes."""
        tree = build_legacy_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        with _without_core():
            placement = clone_and_exec._resolve_work_repo_placement(
                tree.root, PRIMARY_HOST, PRIMARY_PATH
            )
        assert placement.schema is False
        assert placement.base == tree.root / PRIMARY_HOST / PRIMARY_PATH

    def test_a_registered_repo_resolves_to_its_short_name(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        placement = clone_and_exec._resolve_work_repo_placement(
            tree.root, PRIMARY_HOST, PRIMARY_PATH
        )
        assert placement.schema is True
        assert placement.base == tree.project_dir
        assert placement.problem is None

    def test_an_unnamed_repo_resolves_to_its_host_bucket(self, tmp_path, monkeypatch):
        """D9: no short name is invented — the work parks in the bucket."""
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT)
        placement = clone_and_exec._resolve_work_repo_placement(
            tree.root, BUCKET_HOST, BUCKET_PROJECT
        )
        assert placement.schema is True
        assert placement.base == tree.bucket_dir

    def test_a_mirror_remote_resolves_to_the_same_project(self, tmp_path, monkeypatch):
        """Identity is the repo:+remotes: union (D8), which is exactly what
        the mirrored copy this module refuses to grow would have to know."""
        tree = build_schema_plexus(tmp_path / "work")
        mirror = "github.com/example/lmer"
        host, path = mirror.split("/", 1)
        _set_context(monkeypatch, tree.root, host=host, project=path)
        placement = clone_and_exec._resolve_work_repo_placement(tree.root, host, path)
        assert placement.base == tree.project_dir

    def test_an_unreadable_manifest_refuses_to_place_anything(self, tmp_path):
        root = _broken_manifest(tmp_path / "work")
        placement = clone_and_exec._resolve_work_repo_placement(
            root, PRIMARY_HOST, PRIMARY_PATH
        )
        assert placement.schema is True
        assert placement.base is None
        assert placement.problem and "_plexus.yaml" in placement.problem

    def test_a_present_manifest_without_the_core_is_not_a_legacy_tree(self, tmp_path):
        """The one thing that must never happen (D20): a schema tree read at
        the legacy names because this build could not read its manifest."""
        root = tmp_path / "work"
        root.mkdir()
        (root / "_plexus.yaml").write_text("schema: 1\nname: example\n", encoding="utf-8")
        with _without_core():
            placement = clone_and_exec._resolve_work_repo_placement(
                root, PRIMARY_HOST, PRIMARY_PATH
            )
        assert placement.schema is True
        assert placement.base is None
        assert placement.problem and "lmer-schema" in placement.problem

    def test_a_broken_registry_still_parks_the_work_in_the_bucket(self, tmp_path):
        """The document parsed, so only the IDENTITY layer is unusable: a
        fallback WITHIN the form, matching the work CLI and the validator."""
        root = tmp_path / "work"
        root.mkdir()
        (root / "_plexus.yaml").write_text(
            "schema: 1\nname: example\nprojects:\n  lmer:\n    remotes:\n"
            f"      - {PRIMARY_REPO}\n",
            encoding="utf-8",
        )
        placement = clone_and_exec._resolve_work_repo_placement(
            root, PRIMARY_HOST, PRIMARY_PATH
        )
        assert placement.schema is True
        assert placement.base == root / PRIMARY_HOST / PRIMARY_PATH
        assert placement.problem


# ---------------------------------------------------------------------------
# Legacy: byte-for-byte
# ---------------------------------------------------------------------------


class TestLegacyUnchanged:
    """A tree with no manifest gets exactly the chain it always got."""

    @pytest.mark.parametrize(
        "task_type,task_target",
        [
            pytest.param(TASKDEF, TARGET, id="issue-url"),
            pytest.param("review", "feature/some-branch", id="branch"),
            pytest.param(None, None, id="defaults"),
        ],
    )
    def test_matches_the_pre_routing_implementation(
        self, tmp_path, monkeypatch, task_type, task_target
    ):
        routed, reference = tmp_path / "routed", tmp_path / "reference"
        routed.mkdir()
        reference.mkdir()
        _set_context(monkeypatch, routed, host=PRIMARY_HOST, project=PRIMARY_PATH)
        clone_and_exec.ensure_work_repo_directory(
            routed, PRIMARY_HOST, PRIMARY_PATH, task_type, task_target
        )
        _reference_ensure_work_repo_directory(
            reference, PRIMARY_HOST, PRIMARY_PATH, task_type, task_target
        )
        assert _tree_dirs(routed) == _tree_dirs(reference)

    def test_no_host_or_project_creates_nothing(self, tmp_path, monkeypatch):
        root = tmp_path / "work"
        root.mkdir()
        _set_context(monkeypatch, root)
        clone_and_exec.ensure_work_repo_directory(root, None, None, TASKDEF, TARGET)
        assert _tree_dirs(root) == []


# ---------------------------------------------------------------------------
# Schema form: the session dir
# ---------------------------------------------------------------------------


class TestSchemaFormSessionDir:
    """One launch, one complete dir under its group (D22)."""

    def test_creates_the_session_dir_under_its_group(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
        )
        assert (tree.project_dir / "_runs" / SLUG / SESSION_ID).is_dir()

    def test_creates_a_registered_project_dir_from_scratch(self, tmp_path, monkeypatch):
        """A registered project whose dir does not exist yet: the whole chain
        is created, ``_info/`` included (the schema spelling of the legacy
        chain's ``{project}/info/``)."""
        root = tmp_path / "work"
        root.mkdir()
        (root / "_plexus.yaml").write_text(
            f"schema: 1\nname: example\nprojects:\n  lmer:\n    repo: {PRIMARY_REPO}\n",
            encoding="utf-8",
        )
        _set_context(monkeypatch, root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        clone_and_exec.ensure_work_repo_directory(
            root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
        )
        assert (root / "lmer" / "_info").is_dir()
        assert (root / "lmer" / "_runs" / SLUG / SESSION_ID).is_dir()

    def test_writes_none_of_the_legacy_names(self, tmp_path, monkeypatch):
        """A split tree is the failure this routing exists to prevent: no
        ``{host}/{project}`` chain, no ``runs/``, no per-task-type level."""
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
        )
        assert not (tree.root / PRIMARY_HOST / PRIMARY_PATH).exists()
        assert not (tree.project_dir / "runs").exists()
        assert not (tree.project_dir / "info").exists()
        assert not (tree.project_dir / TASKDEF).exists()

    def test_an_unnamed_repo_records_in_its_bucket(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT)
        clone_and_exec.ensure_work_repo_directory(
            tree.root, BUCKET_HOST, BUCKET_PROJECT, TASKDEF, TARGET
        )
        assert (tree.bucket_dir / "_runs" / SLUG / SESSION_ID).is_dir()

    def test_the_group_level_stays_empty_but_for_session_dirs(
        self, tmp_path, monkeypatch
    ):
        """D22: nothing is ever written at ``_runs/<slug>/`` itself."""
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(
            monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH,
            target="https://gitlab.example.com/group/project/-/issues/99",
        )
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF,
            "https://gitlab.example.com/group/project/-/issues/99",
        )
        group = tree.project_dir / "_runs" / "develop-issue-99"
        assert [entry.name for entry in group.iterdir()] == [SESSION_ID]

    @pytest.mark.parametrize(
        "session",
        [
            pytest.param(None, id="unset"),
            pytest.param("", id="empty"),
            pytest.param("..", id="traversal"),
            pytest.param("a/b", id="separator"),
            pytest.param("2026*", id="pattern"),
        ],
    )
    def test_an_id_that_cannot_name_a_dir_opens_no_record(
        self, tmp_path, monkeypatch, session
    ):
        """Never invent a launch identity: an invocation with no usable
        ``LMER_SESSION_ID`` prepares the project dir and stops, exactly as
        the work CLI refuses to compose a record address from such a value."""
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(
            monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH,
            session=session,
            target="https://gitlab.example.com/group/project/-/issues/99",
        )
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF,
            "https://gitlab.example.com/group/project/-/issues/99",
        )
        assert (tree.project_dir / "_info").is_dir()
        assert not (tree.project_dir / "_runs" / "develop-issue-99").exists()

        # ... and the same call with a usable id DOES open one, so the
        # assertion above is about the refusal and not about a step that
        # never runs.
        monkeypatch.setenv("LMER_SESSION_ID", SESSION_ID)
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF,
            "https://gitlab.example.com/group/project/-/issues/99",
        )
        assert (tree.project_dir / "_runs" / "develop-issue-99" / SESSION_ID).is_dir()

    def test_an_unreadable_manifest_creates_nothing_and_says_so(
        self, tmp_path, monkeypatch, capsys
    ):
        root = _broken_manifest(tmp_path / "work")
        _set_context(monkeypatch, root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        clone_and_exec.ensure_work_repo_directory(
            root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
        )
        assert _tree_dirs(root) == []
        assert "_plexus.yaml" in capsys.readouterr().err

    def test_running_twice_is_idempotent(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        for _ in range(2):
            clone_and_exec.ensure_work_repo_directory(
                tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
            )
        assert (tree.project_dir / "_runs" / SLUG / SESSION_ID).is_dir()


class TestAResumeOpensNoSecondDirectory:
    """A launch whose id is a record that already exists creates nothing.

    The host injects the resumed record's id (``lmer --session <id>`` ->
    ``LMER_SESSION_ID``), so this is what container start meets on every
    resume — and the record it names may be a NAMED one, whose directory is
    ``<session-id>--<name>/``. Creating the bare address anyway leaves an
    empty ``<session-id>/`` beside the record the launch is about to write
    in: harmless to routing (no resolver counts a child without a state
    file) and exactly the litter D22 keeps this level free of.
    """

    def _resume(self, tmp_path, monkeypatch, session):
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(
            monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH,
            session=session,
        )
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
        )
        return tree

    def test_a_named_record_gains_no_bare_sibling(self, tmp_path, monkeypatch):
        """The fixture's second launch was named, so its dir is
        ``<id>--second-look`` while ``<id>`` is what an operator resumes by."""
        tree = self._resume(tmp_path, monkeypatch, SESSION_IDS[1])
        assert _dirs_in(tree.runs_dir / SLUG) == sorted(SESSION_DIR_NAMES)

    def test_an_unnamed_record_is_left_exactly_as_it_was(self, tmp_path, monkeypatch):
        tree = self._resume(tmp_path, monkeypatch, SESSION_IDS[0])
        assert _dirs_in(tree.runs_dir / SLUG) == sorted(SESSION_DIR_NAMES)
        assert (tree.runs_dir / SLUG / SESSION_IDS[0] / "state.yaml").is_file()

    def test_a_fresh_launch_still_gets_its_address(self, tmp_path, monkeypatch):
        """The other half of the same call: an id no record answers to is
        created, EMPTY, exactly as before — `work seed`'s rename replaces an
        empty dir at the destination, which is what makes that safe."""
        tree = self._resume(tmp_path, monkeypatch, SESSION_ID)
        opened = tree.runs_dir / SLUG / SESSION_ID
        assert _dirs_in(tree.runs_dir / SLUG) == sorted(
            [*SESSION_DIR_NAMES, SESSION_ID]
        )
        assert list(opened.iterdir()) == []

    def test_an_id_a_record_only_starts_with_is_not_that_record(
        self, tmp_path, monkeypatch
    ):
        """The suffix rule is ``<id>--<name>``, not "starts with the id": a
        sibling launch whose id happens to extend this one is a different
        launch, and treating it as this record's dir would leave the resume
        with no address at all."""
        tree = build_schema_plexus(tmp_path / "work")
        prefix = SESSION_IDS[0][: len(SESSION_IDS[0]) - 1]
        _set_context(
            monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH,
            session=prefix,
        )
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
        )
        assert (tree.runs_dir / SLUG / prefix).is_dir()

    def test_an_empty_dir_of_that_name_is_an_address_not_a_record(
        self, tmp_path, monkeypatch
    ):
        """A directory only becomes a record when it holds state (the rule
        `run_state._session_dirs` and `lmer_platform.workrepo._sessions_in`
        both use), so the address a previous container start prepared is
        prepared again rather than being read as somebody's run."""
        tree = self._resume(tmp_path, monkeypatch, SESSION_ID)
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
        )
        assert (tree.runs_dir / SLUG / SESSION_ID).is_dir()
        assert _dirs_in(tree.runs_dir / SLUG) == sorted(
            [*SESSION_DIR_NAMES, SESSION_ID]
        )


class TestTwoLaunchesOnOneTarget:
    """``_runs/<slug>/`` groups; the session dir is the record (D22).

    Two launches never meet: each prepares its own ``<session-id>/`` under
    the group its (taskdef, target) derives, and the work CLI resolves each
    launch back to its own dir. What the slug rule adds (spec §3) is WHICH
    group a plain session joins — the bare taskdef's, whatever the host
    exported for the target.
    """

    #: Two launch ids in the minted shape, five minutes apart.
    LAUNCHES = ("20260825-120000-4242-31245", "20260825-120500-4243-31246")

    def _launch(self, monkeypatch, tree, session, task, target):
        """One container start, then the dir the work CLI resolves for it."""
        _set_context(
            monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH,
            session=session, task=task, target=target,
        )
        clone_and_exec.ensure_work_repo_directory(
            tree.root, PRIMARY_HOST, PRIMARY_PATH, task, target
        )
        resolved = run_state.run_dir()
        assert resolved is not None and resolved.is_dir()
        return resolved

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param(f"https://{PRIMARY_REPO}.git", id="clone-url"),
            pytest.param(f"https://{PRIMARY_REPO}", id="repo-url"),
            pytest.param(".", id="dot"),
        ],
    )
    def test_two_launches_on_a_repository_target_share_the_bare_group(
        self, tmp_path, monkeypatch, target
    ):
        """#311: the second plain chat on a project is a second record in
        ``_runs/chat/`` — not a ``chat-project.git`` sibling — including the
        ``.`` and bare-URL spellings a lagging host still exports."""
        tree = build_schema_plexus(tmp_path / "work")
        first, second = [
            self._launch(monkeypatch, tree, session, "chat", target)
            for session in self.LAUNCHES
        ]
        group = tree.runs_dir / "chat"
        assert first.parent == group and second.parent == group
        assert first != second
        assert _dirs_in(group) == sorted(self.LAUNCHES)
        assert [name for name in _dirs_in(tree.runs_dir) if name.startswith("chat")] == [
            "chat"
        ]

    def test_two_launches_on_one_work_item_never_share_a_record(
        self, tmp_path, monkeypatch
    ):
        """#117, resolved by design (§9.5 pin): one work-item group, two
        session dirs, and each launch resolves to its own — the record IS the
        session dir, so there is nothing to share and nothing to arbitrate."""
        target = f"https://{PRIMARY_REPO}/-/issues/99"
        tree = build_schema_plexus(tmp_path / "work")
        first, second = [
            self._launch(monkeypatch, tree, session, TASKDEF, target)
            for session in self.LAUNCHES
        ]
        group = tree.runs_dir / "develop-issue-99"
        assert first.parent == group and second.parent == group
        assert {first.name, second.name} == set(self.LAUNCHES)
        assert _dirs_in(group) == sorted(self.LAUNCHES)
        # The second launch neither reused the first's address nor touched
        # it: both are the empty dirs `work seed`'s rename replaces.
        assert list(first.iterdir()) == [] and list(second.iterdir()) == []
        # ... and the first launch still resolves to its own dir afterwards.
        monkeypatch.setenv("LMER_SESSION_ID", self.LAUNCHES[0])
        assert run_state.run_dir() == first


# ---------------------------------------------------------------------------
# The cross-subsystem agreement
# ---------------------------------------------------------------------------


class TestAgreesWithTheWorkCli:
    """The entrypoint and the work CLI must name ONE directory.

    The entrypoint cannot import ``work_repo``, so nothing but a test can
    hold these two together: a drift here means container start prepares a
    dir no ``work`` verb ever writes in, and the launch's record lands
    somewhere nothing reads.
    """

    @pytest.mark.parametrize(
        "host,project",
        [
            pytest.param(PRIMARY_HOST, PRIMARY_PATH, id="registered"),
            pytest.param(BUCKET_HOST, BUCKET_PROJECT, id="bucket"),
        ],
    )
    def test_the_base_is_the_work_cli_base(self, tmp_path, monkeypatch, host, project):
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=host, project=project)
        placement = clone_and_exec._resolve_work_repo_placement(tree.root, host, project)
        assert placement.base == utils.work_repo_layout().base

    @pytest.mark.parametrize(
        "host,project",
        [
            pytest.param(PRIMARY_HOST, PRIMARY_PATH, id="registered"),
            pytest.param(BUCKET_HOST, BUCKET_PROJECT, id="bucket"),
        ],
    )
    def test_the_created_dir_is_the_dir_the_work_cli_resolves(
        self, tmp_path, monkeypatch, host, project
    ):
        tree = build_schema_plexus(tmp_path / "work")
        target = "https://gitlab.example.com/group/project/-/issues/99"
        _set_context(
            monkeypatch, tree.root, host=host, project=project, target=target
        )
        clone_and_exec.ensure_work_repo_directory(
            tree.root, host, project, TASKDEF, target
        )
        resolved = run_state.run_dir()
        assert resolved is not None and resolved.is_dir()
        assert resolved.name == SESSION_ID

    def test_the_legacy_base_is_the_work_cli_base(self, tmp_path, monkeypatch):
        tree = build_legacy_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        placement = clone_and_exec._resolve_work_repo_placement(
            tree.root, PRIMARY_HOST, PRIMARY_PATH
        )
        assert placement.base == utils.work_repo_layout().base

    @pytest.mark.parametrize("task,target", SLUG_TABLE)
    def test_the_slug_is_the_work_cli_slug(self, task, target):
        """The group level is keyed by (taskdef, target) — one derivation or
        the launch prepares a different group than the one `work seed` uses.
        The whole §9.1 table, both halves, through both copies."""
        assert clone_and_exec._derive_run_slug(task, target) == run_state.derive_slug(
            task, target
        )

    @pytest.mark.parametrize("target", BARE_TARGETS)
    def test_no_work_item_derives_the_bare_taskdef(self, target):
        """Spec §3: ``if not is_work_item(target): return taskdef`` — in both
        copies, so ``lmer chat <clone-url>``, ``lmer chat .`` and a plain
        ``lmer chat`` are ONE group, and #81's ``chat-project.git`` is never
        minted again."""
        assert clone_and_exec._derive_run_slug("chat", target) == "chat"
        assert run_state.derive_slug("chat", target) == "chat"

    @pytest.mark.parametrize("target", BARE_TARGETS)
    def test_no_plain_session_slug_carries_a_dot_or_a_repository_basename(self, target):
        """§9.1's negative form of the rule, as an operator reads ``ls
        _runs/``: no ``chat-.``, no ``chat-.-x``, no ``chat-project.git``."""
        basename = (target or "").rstrip("/").rsplit("/", 1)[-1]
        for slug in (
            clone_and_exec._derive_run_slug("chat", target),
            run_state.derive_slug("chat", target),
        ):
            assert "." not in slug
            assert not basename or basename not in slug

    @pytest.mark.parametrize("task,target,slug", WORK_ITEM_SLUGS)
    def test_a_work_item_keeps_today_s_slug(self, task, target, slug):
        """The rule is a guard, not a rewrite: the full-SHA truncation and
        ``sanitize_task_target`` run unchanged behind it, and a thread
        permalink — ``special``, handler-owned — is a work item to it."""
        assert clone_and_exec._derive_run_slug(task, target) == slug
        assert run_state.derive_slug(task, target) == slug

    def test_the_work_cli_classifies_the_env_target_it_falls_back_to(self, monkeypatch):
        """Review S1: ``derive_slug`` resolves ``LMER_TASK_TARGET`` FIRST and
        classifies the value it found — a ``None`` target over a clone URL in
        the env is the bare taskdef, over an issue URL it is the issue's slug.
        The entrypoint's copy has no fallback of its own (its caller read the
        env), so the same ``None`` is bare whatever the env says."""
        monkeypatch.setenv("LMER_TASK_TARGET", f"https://{PRIMARY_REPO}.git")
        assert run_state.derive_slug("chat", None) == "chat"
        assert clone_and_exec._derive_run_slug("chat", None) == "chat"
        monkeypatch.setenv("LMER_TASK_TARGET", TARGET)
        assert run_state.derive_slug(TASKDEF, None) == SLUG
        assert clone_and_exec._derive_run_slug(TASKDEF, None) == TASKDEF

    def test_the_state_file_names_are_the_work_cli_names(self):
        """"Which children are records" turns on these names in three
        modules; the two that can share code do, and this is the third."""
        assert clone_and_exec._STATE_FILENAMES == (
            run_state.STATE_FILE,
            run_state.LEGACY_STATE_FILE,
        )
        assert clone_and_exec._STATE_FILENAMES == workrepo._STATE_FILENAMES

    @pytest.mark.parametrize(
        "occupants",
        [
            pytest.param((), id="empty-group"),
            pytest.param((f"{SESSION_ID}/state.yaml",), id="own-name"),
            pytest.param((f"{SESSION_ID}--named/state.yaml",), id="named"),
            pytest.param((f"{SESSION_ID}--named/state.yml",), id="legacy-state-file"),
            pytest.param((f"{SESSION_ID}/",), id="address-only"),
            pytest.param((f"{SESSION_ID}x/state.yaml",), id="longer-id"),
            pytest.param((f".new-{SESSION_ID}-tmp/state.yaml",), id="seed-mid-rename"),
            pytest.param(("someone-else/state.yaml",), id="another-launch"),
        ],
    )
    def test_a_record_for_this_id_is_the_work_cli_s_answer(
        self, tmp_path, monkeypatch, occupants
    ):
        """The rule mirrored is ``run_state._named_session``: a record is a
        child holding a state file, and it answers to an id by its own name
        or by the ``<id>--<name>`` a freeze rename produced. The entrypoint
        asks the question to decide whether to create an address; the work
        CLI asks it to decide where to write. A drift either way is a stray
        empty dir beside a live record, or a resume with no address at all.
        """
        tree = build_schema_plexus(tmp_path / "work")
        _set_context(monkeypatch, tree.root, host=PRIMARY_HOST, project=PRIMARY_PATH)
        group = tree.root / "empty-group" / SLUG
        group.mkdir(parents=True)
        for occupant in occupants:
            path = group / occupant
            path.parent.mkdir(parents=True, exist_ok=True)
            if not occupant.endswith("/"):
                path.write_text("schema: 1\nslug: x\n", encoding="utf-8")
        assert clone_and_exec._session_record_exists(group, SESSION_ID) == (
            run_state._named_session(group, SESSION_ID) is not None
        )


# ---------------------------------------------------------------------------
# The minted session id
# ---------------------------------------------------------------------------


class TestMintedSessionId:
    """``LMER_SESSION_ID`` is minted here and becomes a directory name there."""

    def test_the_minted_id_can_be_a_directory_name(self, monkeypatch):
        monkeypatch.delenv("LMER_SESSION_ID", raising=False)
        clone_and_exec.mint_session_id()
        minted = os.environ["LMER_SESSION_ID"]
        assert run_state._usable_session_id(minted)
        assert run_state.launch_session_id() == minted

    def test_the_minted_id_is_in_the_schema_session_form(self, monkeypatch):
        """`SESSION_ID_RE` is what the validator and the fleet view read a
        session dir's name with; a minted id that misses it is a record
        nobody can sort, resume or report on."""
        monkeypatch.delenv("LMER_SESSION_ID", raising=False)
        clone_and_exec.mint_session_id()
        minted = os.environ["LMER_SESSION_ID"]
        assert run_state.SESSION_ID_RE.fullmatch(minted)
        assert run_state.SESSION_DIR_RE.fullmatch(f"{minted}--named")

    def test_a_host_injected_id_is_preserved(self, monkeypatch):
        monkeypatch.setenv("LMER_SESSION_ID", SESSION_ID)
        clone_and_exec.mint_session_id()
        assert os.environ["LMER_SESSION_ID"] == SESSION_ID

    @pytest.mark.parametrize(
        "value",
        [
            SESSION_ID,
            f"{SESSION_ID}--a-name",
            "",
            ".",
            "..",
            "a/b",
            "a\\b",
            "2026*",
            "run?",
            "[abc]",
            None,
        ],
    )
    def test_the_usable_id_rule_matches_the_work_cli(self, value):
        """Both sides answer "can this value BE a session dir name" and they
        must answer alike: the entrypoint creates the dir the work CLI later
        refuses to compose, or vice versa."""
        assert clone_and_exec._usable_session_id(value) == run_state._usable_session_id(
            value
        )


class TestMainMintsBeforeItPlaces:
    """A session dir cannot be named by a variable that does not exist yet."""

    def test_the_id_exists_when_the_work_repo_dirs_are_created(self, tmp_path):
        seen = {}

        def fake_ensure(work_repo_path, host, project, task_type, task_target):
            seen["session"] = os.environ.get("LMER_SESSION_ID")

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "LMER_NO_REPO": "1",
            "LMER_WORK_REPO": "https://gitlab.example.com/group/work-repo.git",
            "LMER_WORK_REPO_PATH": str(tmp_path / "work-repo"),
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(clone_and_exec, "ensure_clone"), patch.object(
                clone_and_exec, "ensure_work_repo_directory", fake_ensure
            ), patch.object(clone_and_exec, "provision_documentation"), patch.object(
                clone_and_exec, "find_runner", return_value="/bin/true"
            ), patch.object(
                clone_and_exec, "dispatch_runner", return_value=0
            ):
                rc = clone_and_exec.main(["--", "claude-runner"])

        assert rc == 0
        assert seen.get("session") and run_state._usable_session_id(seen["session"])


class TestStandaloneEntrypoint:
    """The container runs this file BY PATH (``python3 .../clone_and_exec.py``).

    Everything above imports it as a package module, which is exactly the
    context the module's import contract says it may not rely on — so the
    legacy path is exercised once the way the container actually reaches it,
    in a ``-P`` subprocess that loads the file by location and asserts the
    core was never pulled in.
    """

    def test_a_legacy_tree_is_placed_without_the_core(self, tmp_path):
        script = textwrap.dedent(
            f"""
            import importlib.util
            import os
            import sys
            from pathlib import Path

            spec = importlib.util.spec_from_file_location(
                "lmer_container_entrypoint", {str(ENTRYPOINT_PATH)!r}
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules["lmer_container_entrypoint"] = module
            spec.loader.exec_module(module)

            root = Path({str(tmp_path / "work")!r})
            root.mkdir()
            os.environ["LMER_SESSION_ID"] = {SESSION_ID!r}
            module.ensure_work_repo_directory(
                root, "gitlab.example.com", "group/project", "develop", "main"
            )
            assert (root / "gitlab.example.com" / "group" / "project"
                    / "develop" / "main").is_dir()
            assert "lmer_schema" not in sys.modules, "a legacy tree imported the core"
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-P", "-c", script],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
