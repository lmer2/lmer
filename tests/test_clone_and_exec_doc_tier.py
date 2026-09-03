"""The work-repo documentation tier reads the project info area BY FORM (#353).

``clone_and_exec.provision_documentation()`` layers three sources for
``AGENTS.md`` and ``rules/*.md`` — the lmer defaults, then the work repo's
project info area, then the project's own files — and until now it composed
the middle tier itself, as ``{work}/{host}/{project}/info/<doc>``. In a
schema-form tree that path does not exist: the project's info area is
``<project>/_info/`` and the project dir is whatever the REGISTRY resolved,
not the env trio. The tier fell through silently and the agent got the lmer
defaults instead of the project's own conventions — container start even
creates ``<project>/_info/`` (D22) and nothing read it.

What is pinned here:

- **Schema form reads ``<project>/_info/``**, at the registry-resolved
  project dir and at the host bucket of a repo nobody has named yet (D9) —
  and it reads NONE of the legacy names, which in a migrated tree are either
  absent or a stale pre-migration copy.
- **Legacy is byte-for-byte.** The legacy matrix runs the shipped function
  and ``_reference_provision_documentation`` — the pre-routing implementation
  copied VERBATIM — against two identical workspaces and compares everything
  either one leaves behind (files, ``.git/info/exclude``, marker, the
  per-file source labels), rather than a hand-written expectation.
- **A work repo it cannot read never raises and never guesses.** This runs
  on container start's always-succeed path: a present-but-unreadable
  ``_plexus.yaml`` degrades to the lmer defaults, says why, and does not
  read the legacy names for it (D20 — the same split-tree refusal
  ``_resolve_work_repo_placement`` makes for placement).
- **The tier ORDER does not move.** Only where the middle tier looks changed:
  the project's own file still wins over the work repo, which still wins over
  the lmer defaults, in both forms.
"""

import os
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from lmer_cli.container import clone_and_exec
from lmer_cli.container.clone_and_exec import provision_documentation
from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    BUCKET_HOST,
    BUCKET_PROJECT,
    PRIMARY_REPO,
    build_legacy_plexus,
    build_schema_plexus,
)

#: The launch context of the fixture tree's REGISTERED project, as the host
#: CLI exports it (``_parse_host_project`` splits the clone URL into these).
PRIMARY_HOST, PRIMARY_PATH = PRIMARY_REPO.split("/", 1)

#: Distinguishable contents: which tier a provisioned file came from is the
#: whole subject, so no two sources ever write the same bytes.
DEFAULT_AGENTS = "# lmer defaults AGENTS.md\n"
DEFAULT_RULES = "# lmer defaults testing rules\n"
PROJECT_AGENTS = "# the project's own AGENTS.md, from the work repo\n"
PROJECT_RULES = "# the project's own testing rules, from the work repo\n"
LEGACY_DECOY = "# a stale pre-migration copy at the legacy names\n"
WORKSPACE_AGENTS = "# the checked-out project's own AGENTS.md\n"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context: the tier reads the env trio from os.environ."""
    strip_lmer_env(monkeypatch)


def _without_core():
    """Patch context making every ``lmer_schema`` import raise ImportError —
    a build that ships without the core, or with it half-installed.

    Each submodule the resolver names is nulled too: ``from x.y import z``
    consults ``sys.modules["x.y"]`` first (see the twin in
    ``tests/test_clone_and_exec_session_dir.py``).
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


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_git_repo(path):
    """A workspace the way provision_documentation expects one: a git repo
    (it appends to ``.git/info/exclude``)."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


def _make_workspace(path):
    path.mkdir(parents=True, exist_ok=True)
    _init_git_repo(path)
    return path


def _make_global(path, *, agents=DEFAULT_AGENTS, rules=DEFAULT_RULES):
    """The lmer defaults tier (``/Agents/global`` in the container)."""
    _write(path / "AGENTS.md", agents)
    _write(path / "rules" / "testing.md", rules)
    return path


def _snapshot(workspace):
    """Everything a provisioning run leaves behind, as {relpath: text}.

    ``.git`` is skipped except for ``info/exclude``, which the function
    appends to and is therefore part of the observable outcome.
    """
    out = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        parts = rel.parts
        if parts[0] == ".git" and parts[1:] != ("info", "exclude"):
            continue
        out[str(rel)] = path.read_text(encoding="utf-8")
    return out


# The pre-routing implementation of provision_documentation, copied VERBATIM
# (issue #353 predecessor). The legacy matrix compares against this rather
# than a hand-written list of outcomes, so "byte-for-byte" is measured
# against the code that shipped and not against a reading of it.
def _reference_provision_documentation(workspace, work_repo_path, global_path):
    if clone_and_exec._is_self_dev_workspace(workspace):
        print(
            "📄 Self-development mode: skipping documentation provisioning "
            "(workspace is the lmer repo)",
            file=sys.stderr,
        )
        return []

    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")

    doc_files = ["AGENTS.md"]
    global_rules_dir = global_path / "rules"
    if global_rules_dir.is_dir():
        for rule_file in sorted(global_rules_dir.glob("*.md")):
            doc_files.append(f"rules/{rule_file.name}")

    provisioned = []
    sources = {}

    for doc_file in doc_files:
        target = workspace / doc_file
        if target.exists():
            continue

        source = None
        if repo_host and repo_project:
            work_source = work_repo_path / repo_host / repo_project / "info" / doc_file
            if work_source.exists():
                source = work_source
                sources[doc_file] = "work-repo"

        if source is None:
            global_source = global_path / doc_file
            if global_source.exists():
                source = global_source
                sources[doc_file] = "lmer-defaults"

        if source:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))
            provisioned.append(doc_file)

    if provisioned:
        exclude_file = workspace / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text() if exclude_file.exists() else ""
        existing_lines = set(existing.splitlines())
        additions = []
        if "# lmer provisioned documentation" not in existing_lines:
            additions.append("# lmer provisioned documentation")
        for doc_file in provisioned:
            if doc_file not in existing_lines:
                additions.append(doc_file)
        marker_name = ".lmer-provisioned-docs"
        if marker_name not in existing_lines:
            additions.append(marker_name)
        if additions:
            with open(exclude_file, "a") as f:
                f.write("\n" + "\n".join(additions) + "\n")

        marker = workspace / ".lmer-provisioned-docs"
        with open(marker, "w") as f:
            for doc_file in provisioned:
                f.write(f"{doc_file}\n")

        print(f"📄 Provisioned {len(provisioned)} documentation file(s):", file=sys.stderr)
        for doc_file in provisioned:
            src_label = sources.get(doc_file, "unknown")
            print(f"   • {doc_file} ({src_label})", file=sys.stderr)

    return provisioned


# ---------------------------------------------------------------------------
# Schema form: <project>/_info/
# ---------------------------------------------------------------------------


class TestSchemaFormDocTier:
    """The tier the entrypoint creates is the tier the tier reads."""

    def test_the_projects_own_agents_md_reaches_the_agent(
        self, tmp_path, monkeypatch, capsys
    ):
        """The bug: a schema-form tree's ``<project>/_info/AGENTS.md`` was
        never read, so the agent silently got the lmer default instead."""
        tree = build_schema_plexus(tmp_path / "work")
        _write(tree.project_dir / "_info" / "AGENTS.md", PROJECT_AGENTS)
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provisioned = provision_documentation(workspace, tree.root, global_path)

        assert "AGENTS.md" in provisioned
        assert (workspace / "AGENTS.md").read_text() == PROJECT_AGENTS
        assert "AGENTS.md (work-repo)" in capsys.readouterr().err

    def test_the_projects_own_rules_reach_the_agent(self, tmp_path, monkeypatch):
        """``rules/`` layers file by file, so a project overriding one rule
        keeps the lmer default for the rest."""
        tree = build_schema_plexus(tmp_path / "work")
        _write(tree.project_dir / "_info" / "rules" / "testing.md", PROJECT_RULES)
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        _write(global_path / "rules" / "git.md", "# lmer defaults git rules\n")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provisioned = provision_documentation(workspace, tree.root, global_path)

        assert provisioned == ["AGENTS.md", "rules/git.md", "rules/testing.md"]
        assert (workspace / "rules" / "testing.md").read_text() == PROJECT_RULES
        assert (workspace / "rules" / "git.md").read_text() == (
            "# lmer defaults git rules\n"
        )
        assert (workspace / "AGENTS.md").read_text() == DEFAULT_AGENTS

    def test_a_mirror_remote_reads_the_same_project(self, tmp_path, monkeypatch):
        """Identity is the registry's repo:+remotes: union (D8): a launch
        cloned from a mirror gets the project's conventions, not defaults."""
        tree = build_schema_plexus(tmp_path / "work")
        _write(tree.project_dir / "_info" / "AGENTS.md", PROJECT_AGENTS)
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        host, path = "github.com/example/lmer".split("/", 1)
        monkeypatch.setenv("LMER_REPO_HOST", host)
        monkeypatch.setenv("LMER_REPO_PROJECT", path)

        provision_documentation(workspace, tree.root, global_path)

        assert (workspace / "AGENTS.md").read_text() == PROJECT_AGENTS

    def test_an_unnamed_repo_reads_its_host_bucket(self, tmp_path, monkeypatch):
        """D9: a repo nobody has named yet keeps its info in the bucket the
        launch records in — the same dir, so the same docs."""
        tree = build_schema_plexus(tmp_path / "work")
        _write(tree.bucket_dir / "_info" / "AGENTS.md", PROJECT_AGENTS)
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", BUCKET_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", BUCKET_PROJECT)

        provision_documentation(workspace, tree.root, global_path)

        assert (workspace / "AGENTS.md").read_text() == PROJECT_AGENTS

    def test_none_of_the_legacy_names_are_read(self, tmp_path, monkeypatch):
        """A migrated tree's ``{host}/{project}/info/`` is at best a stale
        pre-migration copy; reading it would hand the agent the conventions
        the migration moved away from."""
        tree = build_schema_plexus(tmp_path / "work")
        _write(
            tree.root / PRIMARY_HOST / PRIMARY_PATH / "info" / "AGENTS.md",
            LEGACY_DECOY,
        )
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provision_documentation(workspace, tree.root, global_path)

        assert (workspace / "AGENTS.md").read_text() == DEFAULT_AGENTS

    def test_nothing_in_the_info_area_falls_back_to_the_defaults(
        self, tmp_path, monkeypatch, capsys
    ):
        tree = build_schema_plexus(tmp_path / "work")
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provisioned = provision_documentation(workspace, tree.root, global_path)

        assert provisioned == ["AGENTS.md", "rules/testing.md"]
        assert (workspace / "AGENTS.md").read_text() == DEFAULT_AGENTS
        assert "AGENTS.md (lmer-defaults)" in capsys.readouterr().err

    def test_the_checked_out_projects_own_file_still_wins(
        self, tmp_path, monkeypatch
    ):
        """Tier order is unchanged: what is already in the workspace is the
        project's own and is never overwritten, in either form."""
        tree = build_schema_plexus(tmp_path / "work")
        _write(tree.project_dir / "_info" / "AGENTS.md", PROJECT_AGENTS)
        workspace = _make_workspace(tmp_path / "workspace")
        _write(workspace / "AGENTS.md", WORKSPACE_AGENTS)
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provisioned = provision_documentation(workspace, tree.root, global_path)

        assert "AGENTS.md" not in provisioned
        assert (workspace / "AGENTS.md").read_text() == WORKSPACE_AGENTS


# ---------------------------------------------------------------------------
# Legacy: byte-for-byte
# ---------------------------------------------------------------------------


class TestLegacyUnchanged:
    """A tree with no manifest is provisioned exactly as it always was."""

    def _both(self, tmp_path, work_repo, global_path):
        """Run the shipped function and the verbatim pre-routing copy against
        two identical workspaces; return (routed, reference) outcomes."""
        outcomes = []
        for name, func in (
            ("routed", provision_documentation),
            ("reference", _reference_provision_documentation),
        ):
            workspace = _make_workspace(tmp_path / name)
            provisioned = func(workspace, work_repo, global_path)
            outcomes.append((provisioned, _snapshot(workspace)))
        return outcomes

    def test_the_work_repo_tier_is_the_same_path(self, tmp_path, monkeypatch):
        tree = build_legacy_plexus(tmp_path / "work")
        _write(tree.info_dir / "AGENTS.md", PROJECT_AGENTS)
        _write(tree.info_dir / "rules" / "testing.md", PROJECT_RULES)
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        routed, reference = self._both(tmp_path, tree.root, global_path)

        assert routed == reference
        assert routed[1]["AGENTS.md"] == PROJECT_AGENTS
        assert routed[1]["rules/testing.md"] == PROJECT_RULES

    def test_an_empty_work_repo_falls_back_the_same_way(self, tmp_path, monkeypatch):
        tree = build_legacy_plexus(tmp_path / "work")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        routed, reference = self._both(tmp_path, tree.root, global_path)

        assert routed == reference
        assert routed[1]["AGENTS.md"] == DEFAULT_AGENTS

    def test_a_bare_work_repo_dir_falls_back_the_same_way(self, tmp_path, monkeypatch):
        """The commonest tree of all: no manifest and no project dir yet."""
        work_repo = tmp_path / "work"
        work_repo.mkdir()
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        routed, reference = self._both(tmp_path, work_repo, global_path)

        assert routed == reference
        assert routed[1]["AGENTS.md"] == DEFAULT_AGENTS

    def test_no_run_context_falls_back_the_same_way(self, tmp_path, monkeypatch):
        """With no ``LMER_REPO_HOST``/``LMER_REPO_PROJECT`` there is no
        project to read info for; the defaults answer, as they always did."""
        tree = build_legacy_plexus(tmp_path / "work")
        _write(tree.info_dir / "AGENTS.md", PROJECT_AGENTS)
        global_path = _make_global(tmp_path / "global")
        monkeypatch.delenv("LMER_REPO_HOST", raising=False)
        monkeypatch.delenv("LMER_REPO_PROJECT", raising=False)

        routed, reference = self._both(tmp_path, tree.root, global_path)

        assert routed == reference
        assert routed[1]["AGENTS.md"] == DEFAULT_AGENTS

    def test_nothing_to_provision_leaves_the_same_nothing(
        self, tmp_path, monkeypatch
    ):
        """No marker, no exclude entry, no empty-run side effects."""
        tree = build_legacy_plexus(tmp_path / "work")
        global_path = tmp_path / "global"
        global_path.mkdir()
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        routed, reference = self._both(tmp_path, tree.root, global_path)

        assert routed == reference
        assert routed[0] == []
        assert ".lmer-provisioned-docs" not in routed[1]

    def test_a_legacy_tree_needs_no_core_at_all(self, tmp_path, monkeypatch):
        """The entrypoint stays stdlib-only on the path every unmigrated tree
        takes: the manifest presence probe answers before any import."""
        tree = build_legacy_plexus(tmp_path / "work")
        _write(tree.info_dir / "AGENTS.md", PROJECT_AGENTS)
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        with _without_core():
            provision_documentation(workspace, tree.root, global_path)

        assert (workspace / "AGENTS.md").read_text() == PROJECT_AGENTS

    def test_the_self_dev_skip_is_untouched(self, tmp_path, monkeypatch):
        """Self-dev returns before any work-repo question is asked, so the
        routing cannot have moved that guard."""
        tree = build_schema_plexus(tmp_path / "work")
        _write(tree.project_dir / "_info" / "AGENTS.md", PROJECT_AGENTS)
        workspace = _make_workspace(tmp_path / "workspace")
        _write(
            workspace / "pyproject.toml",
            '[project]\nname = "lmer"\nversion = "0.0.0"\n',
        )
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        assert provision_documentation(workspace, tree.root, global_path) == []
        assert not (workspace / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# A work repo this build cannot read
# ---------------------------------------------------------------------------


class TestUnreadableWorkRepo:
    """Container start's always-succeed path: degrade, say why, never guess."""

    def _broken_manifest_tree(self, root):
        """A tree whose ``_plexus.yaml`` is present and unparseable, with a
        legacy-named info dir holding a decoy — the thing that must NOT be
        read for it (D20)."""
        root.mkdir(parents=True, exist_ok=True)
        _write(root / "_plexus.yaml", "schema: 1\nprojects: [oops\n")
        _write(root / PRIMARY_HOST / PRIMARY_PATH / "info" / "AGENTS.md", LEGACY_DECOY)
        return root

    def test_an_unreadable_manifest_provisions_the_defaults_and_says_why(
        self, tmp_path, monkeypatch, capsys
    ):
        root = self._broken_manifest_tree(tmp_path / "work")
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provisioned = provision_documentation(workspace, root, global_path)

        assert provisioned == ["AGENTS.md", "rules/testing.md"]
        assert (workspace / "AGENTS.md").read_text() == DEFAULT_AGENTS
        stderr = capsys.readouterr().err
        assert "_plexus.yaml" in stderr
        assert "AGENTS.md (lmer-defaults)" in stderr

    def test_a_present_manifest_without_the_core_reads_no_legacy_names(
        self, tmp_path, monkeypatch, capsys
    ):
        """A build that cannot import the core cannot tell which project dir
        this launch belongs to, and a guess at the legacy names would hand
        the agent a pre-migration copy of someone's conventions."""
        root = tmp_path / "work"
        root.mkdir()
        _write(root / "_plexus.yaml", "schema: 1\nname: example\n")
        _write(root / PRIMARY_HOST / PRIMARY_PATH / "info" / "AGENTS.md", LEGACY_DECOY)
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        with _without_core():
            provision_documentation(workspace, root, global_path)

        assert (workspace / "AGENTS.md").read_text() == DEFAULT_AGENTS
        assert "lmer-schema" in capsys.readouterr().err

    def test_a_broken_registry_still_reads_the_host_bucket(
        self, tmp_path, monkeypatch
    ):
        """The document parsed, so only IDENTITY is unusable: the info comes
        from the bucket the work parks in, a fallback WITHIN the form."""
        root = tmp_path / "work"
        root.mkdir()
        _write(
            root / "_plexus.yaml",
            "schema: 1\nname: example\nprojects:\n  lmer:\n    remotes:\n"
            f"      - {PRIMARY_REPO}\n",
        )
        _write(
            root / PRIMARY_HOST / PRIMARY_PATH / "_info" / "AGENTS.md", PROJECT_AGENTS
        )
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provision_documentation(workspace, root, global_path)

        assert (workspace / "AGENTS.md").read_text() == PROJECT_AGENTS

    def test_an_unreadable_work_repo_never_raises(self, tmp_path, monkeypatch):
        """The caller wraps this in a warn-and-continue, but a container that
        starts with no AGENTS.md at all is a session with no conventions —
        the degrade belongs here."""
        root = self._broken_manifest_tree(tmp_path / "work")
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        with _without_core():
            assert provision_documentation(workspace, root, global_path) == [
                "AGENTS.md",
                "rules/testing.md",
            ]

    def test_a_work_repo_that_does_not_exist_provisions_the_defaults(
        self, tmp_path, monkeypatch
    ):
        """``LMER_WORK_REPO_PATH`` pointing at nothing (a launch with no work
        repo cloned) is a fall-through, not a failure."""
        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
        monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)

        provisioned = provision_documentation(
            workspace, tmp_path / "no-such-work-repo", global_path
        )

        assert provisioned == ["AGENTS.md", "rules/testing.md"]
        assert (workspace / "AGENTS.md").read_text() == DEFAULT_AGENTS


# ---------------------------------------------------------------------------
# The cross-subsystem agreement
# ---------------------------------------------------------------------------


class TestAgreesWithPlacement:
    """The dir container start CREATES is the dir the doc tier READS.

    ``ensure_work_repo_directory`` makes ``<project>/_info/`` on every
    schema-form launch (D22). If the tier looked anywhere else, that dir
    would be the empty directory this bug was found as.
    """

    @pytest.mark.parametrize(
        "host,project",
        [
            pytest.param(PRIMARY_HOST, PRIMARY_PATH, id="registered"),
            pytest.param(BUCKET_HOST, BUCKET_PROJECT, id="bucket"),
        ],
    )
    def test_the_tier_is_the_info_dir_container_start_creates(
        self, tmp_path, monkeypatch, host, project
    ):
        root = tmp_path / "work"
        root.mkdir()
        _write(
            root / "_plexus.yaml",
            f"schema: 1\nname: example\nprojects:\n  lmer:\n    repo: {PRIMARY_REPO}\n",
        )
        monkeypatch.setenv("LMER_REPO_HOST", host)
        monkeypatch.setenv("LMER_REPO_PROJECT", project)
        monkeypatch.setenv("LMER_SESSION_ID", "20260825-120000-4242-31245")
        clone_and_exec.ensure_work_repo_directory(
            root, host, project, "develop", "main"
        )
        placement = clone_and_exec._resolve_work_repo_placement(root, host, project)
        created_info = placement.base / "_info"
        assert created_info.is_dir()
        _write(created_info / "AGENTS.md", PROJECT_AGENTS)

        workspace = _make_workspace(tmp_path / "workspace")
        global_path = _make_global(tmp_path / "global")
        provision_documentation(workspace, root, global_path)

        assert (workspace / "AGENTS.md").read_text() == PROJECT_AGENTS
