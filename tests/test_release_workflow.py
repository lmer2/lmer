"""Structural invariants of .github/workflows/release.yml.

The release path is four jobs — checks, build, publish-pypi, github-release —
and its safety rests on structure rather than on scripts: the privileged job
runs no repository code, `.github/scripts/` does not exist so a pushed tag has
nothing to substitute, and the two third-party actions are SHA-pinned. These
tests assert those facts against the parsed workflow, so an edit that weakens
the shape fails here instead of on a tag that cannot be re-cut.
"""

import re
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
CHECKS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "checks.yml"
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"

PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
RELEASE_ACTION = "softprops/action-gh-release"
PINNED_ACTIONS = (PUBLISH_ACTION, RELEASE_ACTION)

# The job graph, in order. `checks` is the repository's existing CI workflow
# called on the tagged commit; everything else hangs off it in a straight line.
JOB_ORDER = ["checks", "build", "publish-pypi", "github-release"]

# v1.12.0 is where gh-action-pypi-publish started emitting PEP 740
# attestations by default; the pin must never fall below it. v1.14.2 is the
# first release whose twine accepts Metadata-Version 2.5 (ctl #44), which is
# why the floor sits there rather than at 1.12.
PUBLISH_ACTION_FLOOR = (1, 14, 2)


@pytest.fixture(scope="module")
def gate():
    """ci/publisher_metadata_gate.py, imported as a module."""
    sys.path.insert(0, str(REPO_ROOT / "ci"))
    try:
        import publisher_metadata_gate

        return publisher_metadata_gate
    finally:
        sys.path.pop(0)


@pytest.fixture(scope="module")
def workflow_text():
    return WORKFLOW.read_text()


@pytest.fixture(scope="module")
def workflow(workflow_text):
    return yaml.safe_load(workflow_text)


@pytest.fixture(scope="module")
def jobs(workflow):
    return workflow["jobs"]


def _needs(job):
    """A job's needs as a list (GitHub accepts a bare string or a list)."""
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _step_using(job, action):
    """The single step of `job` that uses `action`."""
    steps = [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).split("@")[0] == action
    ]
    assert len(steps) == 1, f"expected exactly one {action} step, found {len(steps)}"
    return steps[0]


class TestJobGraph:
    def test_jobs_are_exactly_the_four_release_jobs(self, jobs):
        assert list(jobs) == JOB_ORDER

    def test_each_job_needs_the_one_before_it(self, jobs):
        for earlier, later in zip(JOB_ORDER, JOB_ORDER[1:]):
            assert _needs(jobs[later]) == [earlier], (
                f"'{later}' must need '{earlier}' and nothing else"
            )

    def test_checks_is_the_root_and_calls_the_ci_workflow(self, jobs):
        assert "needs" not in jobs["checks"]
        assert jobs["checks"]["uses"] == "./.github/workflows/checks.yml"

    def test_the_called_workflow_accepts_workflow_call(self):
        """`uses:` only resolves if checks.yml declares the trigger."""
        checks = yaml.safe_load(CHECKS_WORKFLOW.read_text())
        # PyYAML parses the bare key `on:` as the boolean True.
        triggers = checks.get("on", checks.get(True))
        assert "workflow_call" in triggers

    def test_release_triggers_on_version_tags_only(self, workflow):
        triggers = workflow.get("on", workflow.get(True))
        assert list(triggers) == ["push"]
        assert triggers["push"] == {"tags": ["v*"]}


class TestPermissions:
    def test_workflow_level_is_read_only(self, workflow):
        assert workflow["permissions"] == {"contents": "read"}

    def test_publish_holds_only_the_oidc_token(self, jobs):
        """A job-level block REPLACES the workflow-level one rather than
        merging with it, so anything extra here is a real grant to the job
        that mints the OIDC token."""
        assert jobs["publish-pypi"]["permissions"] == {"id-token": "write"}

    def test_github_release_holds_contents_write_and_actions_read(self, jobs):
        assert jobs["github-release"]["permissions"] == {
            "contents": "write",
            "actions": "read",
        }

    def test_no_other_job_declares_permissions(self, jobs):
        for name in ("checks", "build"):
            assert "permissions" not in jobs[name], (
                f"'{name}' needs no token; the workflow-level read is enough"
            )


class TestPrivilegedJobRunsNoRepositoryCode:
    def test_scripts_directory_does_not_exist(self):
        """With nothing in .github/scripts/ a pushed tag has no repository
        code to substitute into a privileged job."""
        assert not SCRIPTS_DIR.exists(), f"{SCRIPTS_DIR} is back"

    def test_publish_job_never_checks_out(self, jobs):
        uses = [str(step.get("uses", "")) for step in jobs["publish-pypi"]["steps"]]
        assert not any(u.split("@")[0] == "actions/checkout" for u in uses)

    def test_publish_job_runs_no_shell(self, jobs):
        assert not any("run" in step for step in jobs["publish-pypi"]["steps"])

    def test_workflow_pins_no_checkout_to_main(self, workflow_text):
        """The `ref: main` contortions existed to keep tag-borne code out of
        privileged jobs; no job holds a token and a checkout any more."""
        assert "ref: main" not in workflow_text

    def test_workflow_names_no_repository_variable(self, workflow_text):
        assert "vars." not in workflow_text

    def test_no_reference_to_the_retired_gates(self, workflow_text):
        for retired in (
            ".github/scripts",
            "verify-tag-signature",
            "gate-version-reuse",
            "RELEASE_ALLOWED_SIGNERS",
            "RELEASE_RESUME_VERSION",
        ):
            assert retired not in workflow_text


class TestBuildJob:
    def test_build_fetches_full_history(self, jobs):
        checkout = _step_using(jobs["build"], "actions/checkout")
        assert checkout["with"]["fetch-depth"] == 0

    def test_build_checks_the_tag_against_pyproject(self, jobs):
        """setuptools-scm is not adopted yet (#335), so the tag and the
        static version must be compared somewhere."""
        runs = "\n".join(
            str(step.get("run", "")) for step in jobs["build"]["steps"]
        )
        assert "GITHUB_REF_NAME" in runs
        assert "pyproject.toml" in runs

    def test_build_sets_source_date_epoch(self, jobs):
        runs = "\n".join(
            str(step.get("run", "")) for step in jobs["build"]["steps"]
        )
        assert "SOURCE_DATE_EPOCH" in runs

    def test_build_runs_both_pre_publish_gates(self, jobs):
        """Reaching publish-pypi red spends the version and the tag; these
        two gates are what stop the run before that."""
        runs = [str(step.get("run", "")) for step in jobs["build"]["steps"]]
        for gate in ("ci/check_build_constraint.py", "ci/publisher_metadata_gate.py"):
            assert any(gate in run for run in runs), f"{gate} is not run by build"
            assert (REPO_ROOT / gate).is_file()

    def test_gates_run_after_the_build_and_before_the_upload(self, jobs):
        steps = jobs["build"]["steps"]
        build_idx = next(
            i for i, s in enumerate(steps) if str(s.get("run", "")).strip() == "uv build"
        )
        upload_idx = next(
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).split("@")[0] == "actions/upload-artifact"
        )
        gate_idxs = [
            i for i, s in enumerate(steps) if "ci/" in str(s.get("run", ""))
        ]
        assert gate_idxs
        assert build_idx < min(gate_idxs)
        assert max(gate_idxs) < upload_idx

    def test_upload_fails_on_an_empty_dist(self, jobs):
        upload = _step_using(jobs["build"], "actions/upload-artifact")
        assert upload["with"]["if-no-files-found"] == "error"


class TestPublishStep:
    def test_attestations_are_stated_explicitly(self, jobs):
        step = _step_using(jobs["publish-pypi"], PUBLISH_ACTION)
        assert step["with"]["attestations"] is True

    def test_skip_existing_is_absent(self, jobs):
        """PyPI refusing an upload for a version it already holds IS the
        version-reuse gate; swallowing that refusal is what the deleted
        gate script existed to compensate for."""
        step = _step_using(jobs["publish-pypi"], PUBLISH_ACTION)
        assert "skip-existing" not in step["with"]

    def test_publish_environment_is_the_reviewed_pypi_environment(self, jobs):
        environment = jobs["publish-pypi"]["environment"]
        assert environment["name"] == "pypi"
        assert re.fullmatch(
            r"https://pypi\.org/project/[^/]+/", environment["url"]
        )


class TestGithubReleaseJob:
    def test_download_passes_token_and_run_id(self, jobs):
        """Without these the download uses the attempt-sensitive internal
        backend, which 404s when only this job is re-run after a successful
        publish (actions/download-artifact#486)."""
        step = _step_using(jobs["github-release"], "actions/download-artifact")
        assert step["with"]["github-token"] == "${{ secrets.GITHUB_TOKEN }}"
        assert step["with"]["run-id"] == "${{ github.run_id }}"

    def test_release_uploads_the_built_distributions(self, jobs):
        step = _step_using(jobs["github-release"], RELEASE_ACTION)
        assert step["with"]["files"] == "dist/*"
        assert step["with"]["fail_on_unmatched_files"] is True


class TestActionPinning:
    """Both third-party actions are SHA-pinned with a version comment; the
    two move together, deliberately, in one edit."""

    def _pin_line(self, workflow_text, action):
        lines = [
            line
            for line in workflow_text.splitlines()
            if f"{action}@" in line and not line.lstrip().startswith("#")
        ]
        assert len(lines) == 1, f"expected one {action} pin, found {len(lines)}"
        return lines[0]

    @pytest.mark.parametrize("action", PINNED_ACTIONS)
    def test_pinned_to_a_full_commit_sha(self, workflow_text, action):
        ref = self._pin_line(workflow_text, action).split(f"{action}@", 1)[1].split()[0]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"{action} ref {ref!r} is not a full commit SHA pin"
        )

    @pytest.mark.parametrize("action", PINNED_ACTIONS)
    def test_pin_carries_a_version_comment(self, workflow_text, action):
        line = self._pin_line(workflow_text, action)
        assert re.search(r"#\s*v\d+\.\d+\.\d+\s*$", line), (
            f"{action} pin lacks the '# vX.Y.Z' version comment: {line.strip()!r}"
        )

    def test_publish_action_pin_meets_the_metadata_floor(self, workflow_text):
        line = self._pin_line(workflow_text, PUBLISH_ACTION)
        match = re.search(r"#\s*v(\d+)\.(\d+)\.(\d+)\s*$", line)
        version = tuple(int(part) for part in match.groups())
        assert version >= PUBLISH_ACTION_FLOOR, (
            f"publish action pinned at v{'.'.join(match.groups())}, below the "
            f"floor v{'.'.join(map(str, PUBLISH_ACTION_FLOOR))} whose twine "
            "accepts Metadata-Version 2.5"
        )

    def test_the_metadata_gate_reads_this_same_pin(self, workflow_text, gate):
        """The gate derives the publisher's tooling from the workflow pin
        rather than from a copy of the versions, so the two cannot drift."""
        pinned = self._pin_line(workflow_text, PUBLISH_ACTION)
        assert gate.publisher_sha() in pinned


class TestBuildConstraint:
    def test_pyproject_pins_the_build_backend(self):
        """build-system.requires resolves outside uv.lock, so this pin is
        the only thing fixing the backend that decides our metadata
        version. ci/check_build_constraint.py asserts it actually bound."""
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        constraints = pyproject["tool"]["uv"]["build-constraint-dependencies"]
        assert any(c.replace(" ", "").startswith("setuptools==") for c in constraints)


class TestMetadataGateIdentity:
    """The gate reads its identity from the repository it runs in, so no
    second spelling of the project name can fall out of step with
    `[project]` in pyproject.toml."""

    def test_project_name_comes_from_pyproject(self, gate):
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            declared = tomllib.load(handle)["project"]["name"]
        assert gate.project_name() == declared

    def test_no_project_name_is_hard_coded(self, gate):
        source = (REPO_ROOT / "ci" / "publisher_metadata_gate.py").read_text()
        assert 'PROJECT = "' not in source

    def test_distribution_match_normalises_separators(self, gate, tmp_path, monkeypatch):
        """PEP 503/427: `a-b` builds `a_b-<version>-...`. A plain
        `glob(f"{name}-*")` would find nothing for such a project."""
        dist = tmp_path / "dist"
        dist.mkdir()
        for name in (
            "my_pkg-0.0.0-py3-none-any.whl",
            "my_pkg-0.0.0.tar.gz",
            "something_else-1.0.tar.gz",
        ):
            (dist / name).write_bytes(b"")
        monkeypatch.setattr(gate, "REPO", tmp_path)
        monkeypatch.setattr(gate, "project_name", lambda: "my-pkg")
        found = [Path(p).name for p in gate.built_distributions()]
        assert found == [
            "my_pkg-0.0.0-py3-none-any.whl",
            "my_pkg-0.0.0.tar.gz",
        ]

    def test_missing_dist_directory_is_not_a_crash(self, gate, tmp_path, monkeypatch):
        monkeypatch.setattr(gate, "REPO", tmp_path)
        assert gate.built_distributions() == []
