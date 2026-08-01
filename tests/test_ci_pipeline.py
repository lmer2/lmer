"""The tag pipeline publishes the image the build stage verified (#189).

Two kinds of test here, because the property has two halves.

``scripts/ci-image.sh`` is exercised for real against a stubbed ``docker``: it is
the piece that decides what gets published, and its interesting behaviour is the
refusals — an image that was never pushed, a digest that arrived empty because
the dotenv did not propagate, a published manifest that came back different from
the built one. Those paths never run in a green pipeline, so a green pipeline is
no evidence about them.

The rest are source guards over ``.gitlab-ci.yml``. They pin the shape the fix
depends on — the publish jobs build nothing, each image is built once, and the
variable the publish job reads is the one the build job writes — because the
regression they guard against is invisible: a ``docker build`` restored to a
publish job produces a perfectly green pipeline that publishes an unverified
artifact, which is exactly how #189 shipped in the first place.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
CI_FILE = REPO_ROOT / ".gitlab-ci.yml"
CI_IMAGE_SH = REPO_ROOT / "scripts" / "ci-image.sh"

REPO = "registry.example.com/group/proj"
DIGEST_A = "sha256:" + "a1" * 32
DIGEST_B = "sha256:" + "b2" * 32


# --- running scripts/ci-image.sh against a stubbed docker ---------------------

DOCKER_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys

    with open(os.environ["STUB_LOG"], "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")

    if sys.argv[1:3] == ["image", "inspect"]:
        sys.stdout.write(os.environ.get("STUB_INSPECT", ""))

    sys.exit(int(os.environ.get("STUB_EXIT", "0")))
    """
)


@pytest.fixture
def docker():
    """A ``docker`` on PATH that records its arguments and answers ``inspect``.

    Returns a callable ``run(*args, inspect=..., ...)`` plus a ``calls`` list, so
    a test can assert on what the script asked the daemon to do as well as on
    what it exited with.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        bindir = tmpdir / "bin"
        bindir.mkdir()
        stub = bindir / "docker"
        stub.write_text(DOCKER_STUB, encoding="utf-8")
        stub.chmod(0o755)
        log = tmpdir / "calls.log"
        log.touch()

        class Docker:
            def run(self, *args, inspect=""):
                env = dict(os.environ)
                env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
                env["STUB_LOG"] = str(log)
                env["STUB_INSPECT"] = inspect
                return subprocess.run(
                    ["sh", str(CI_IMAGE_SH), *args],
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=REPO_ROOT,
                )

            @property
            def calls(self):
                return log.read_text(encoding="utf-8").splitlines()

        yield Docker()


def repo_digest_lines(*entries):
    """What ``docker image inspect`` prints for the RepoDigests range."""
    return "".join(f"{entry}\n" for entry in entries)


def test_digest_reports_the_registry_digest_as_a_dotenv_line(docker):
    """The build job's whole output: one ``VAR=sha256:…`` line for the publish job."""
    result = docker.run(
        "digest", REPO, "abc1234", "SESSION_IMAGE_DIGEST",
        inspect=repo_digest_lines(f"{REPO}@{DIGEST_A}"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"SESSION_IMAGE_DIGEST={DIGEST_A}\n"


def test_digest_ignores_digests_belonging_to_another_repository(docker):
    """RepoDigests is a property of the image, not of the tag that was pushed.

    An image tagged into two repositories lists both, in an order nothing
    promises — which is why the script filters by repository instead of taking
    ``{{index .RepoDigests 0}}``. Here the foreign entry is listed first, so a
    positional read would publish the wrong digest.
    """
    result = docker.run(
        "digest", REPO, "abc1234", "SESSION_IMAGE_DIGEST",
        inspect=repo_digest_lines(f"other.example.com/mirror@{DIGEST_B}", f"{REPO}@{DIGEST_A}"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"SESSION_IMAGE_DIGEST={DIGEST_A}\n"


def test_digest_refuses_an_image_that_was_never_pushed(docker):
    """A built-but-unpushed image has no RepoDigests at all.

    Reporting nothing here would hand the publish job an empty digest, which is
    the failure the format check downstream exists to catch — but it is cheaper
    and clearer to fail in the job that actually knows the push did not happen.
    """
    result = docker.run("digest", REPO, "abc1234", "SESSION_IMAGE_DIGEST", inspect="")
    assert result.returncode != 0
    assert "was it pushed?" in result.stderr
    assert result.stdout == ""


def test_digest_refuses_an_image_holding_two_digests_for_the_repository(docker):
    """Two manifests for one repository is the divergence this change prevents.

    Picking one would be a guess about which of them the release tags point at,
    so it is an error instead — and both are named, since which pair they are is
    the whole diagnostic.
    """
    result = docker.run(
        "digest", REPO, "abc1234", "SESSION_IMAGE_DIGEST",
        inspect=repo_digest_lines(f"{REPO}@{DIGEST_A}", f"{REPO}@{DIGEST_B}"),
    )
    assert result.returncode != 0
    assert "more than one digest" in result.stderr
    assert DIGEST_A in result.stderr and DIGEST_B in result.stderr


def test_promote_publishes_the_built_digest_under_every_release_tag(docker):
    """The fix itself: pull by digest, re-tag, push — never build."""
    result = docker.run(
        "promote", REPO, DIGEST_A, "v1.2.3", "latest",
        inspect=repo_digest_lines(f"{REPO}@{DIGEST_A}"),
    )
    assert result.returncode == 0, result.stderr

    assert f"pull {REPO}@{DIGEST_A}" in docker.calls
    for tag in ("v1.2.3", "latest"):
        assert f"tag {REPO}@{DIGEST_A} {REPO}:{tag}" in docker.calls
        assert f"push {REPO}:{tag}" in docker.calls
    assert not any(call.startswith("build") for call in docker.calls)


def test_promote_logs_both_digests_side_by_side_on_one_line(docker):
    """#189 asks for the comparison to be demonstrated, not assumed.

    On one line, because the job log is where this record survives — a built
    digest on one line and a published one forty lines later is a thing a reader
    has to reassemble before they can see whether it held.
    """
    result = docker.run(
        "promote", REPO, DIGEST_A, "v1.2.3",
        inspect=repo_digest_lines(f"{REPO}@{DIGEST_A}"),
    )
    assert result.returncode == 0, result.stderr
    ok_lines = [line for line in result.stdout.splitlines() if line.startswith("ci-image.sh: OK ")]
    assert ok_lines == [f"ci-image.sh: OK {REPO}:v1.2.3  built={DIGEST_A}  published={DIGEST_A}"]


def test_promote_is_idempotent_so_a_partial_failure_can_be_re_run(docker):
    """A publish job that dies between two tag pushes has to be safe to re-run.

    Pull-by-digest, ``docker tag`` and a push of an identical manifest are each
    idempotent, so the re-run redoes the whole promotion rather than resuming
    into a half-tagged state. Asserted rather than reasoned about, because it is
    a property of the sequence and any future step that recorded state or
    skipped work "because it already ran" would break it silently.
    """
    inspect = repo_digest_lines(f"{REPO}@{DIGEST_A}")
    first = docker.run("promote", REPO, DIGEST_A, "v1.2.3", "latest", inspect=inspect)
    calls_after_first = list(docker.calls)
    second = docker.run("promote", REPO, DIGEST_A, "v1.2.3", "latest", inspect=inspect)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    # The second run issues the same sequence again — nothing is skipped, and
    # nothing extra is attempted to "recover".
    assert docker.calls[len(calls_after_first):] == calls_after_first


def test_promote_fails_when_the_published_digest_is_not_the_built_one(docker):
    """Re-tagging cannot change content, but pushing writes a fresh manifest.

    Nothing published promises the daemon regenerates that manifest byte for
    byte, so the job asks the registry what the tag resolves to rather than
    assuming. This is that check firing.
    """
    result = docker.run(
        "promote", REPO, DIGEST_A, "v1.2.3",
        inspect=repo_digest_lines(f"{REPO}@{DIGEST_B}"),
    )
    assert result.returncode != 0
    assert "FAIL" in result.stderr
    assert DIGEST_A in result.stderr and DIGEST_B in result.stderr


@pytest.mark.parametrize(
    "digest",
    [
        pytest.param("", id="empty-dotenv-did-not-propagate"),
        pytest.param("sha256:abc", id="too-short"),
        pytest.param("sha256:" + "z" * 64, id="not-hex"),
        pytest.param("latest", id="a-tag-not-a-digest"),
    ],
)
def test_promote_refuses_anything_that_is_not_a_digest_before_touching_docker(docker, digest):
    """An unset dotenv variable arrives as the empty string.

    ``docker pull "$repo@"`` is not an error the daemon reports as one — the
    reference degrades to the bare repository, i.e. ``:latest``, and the job
    would cheerfully re-publish whatever ``latest`` already was. Refusing before
    any docker call is what keeps a lost variable from becoming a silent
    mis-publish.
    """
    result = docker.run("promote", REPO, digest, "v1.2.3", inspect="")
    assert result.returncode != 0
    assert "is not a sha256 digest" in result.stderr
    assert docker.calls == []


# --- source guards over .gitlab-ci.yml ---------------------------------------


@pytest.fixture(scope="module")
def pipeline():
    assert CI_FILE.is_file(), f"{CI_FILE} is missing"
    return yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))


BUILD_JOBS = ("build-container", "build-platform-container")
PUBLISH_JOBS = ("publish-container", "publish-platform-container")

# publish job -> (build job it promotes from, the dotenv variable carrying the digest)
PROMOTION = {
    "publish-container": ("build-container", "SESSION_IMAGE_DIGEST"),
    "publish-platform-container": ("build-platform-container", "PLATFORM_IMAGE_DIGEST"),
}


def script_of(pipeline, job):
    return "\n".join(pipeline[job]["script"])


@pytest.mark.parametrize("job", PUBLISH_JOBS)
def test_publish_jobs_do_not_build(pipeline, job):
    """The regression that is invisible in a green pipeline.

    A publish job that builds its own copy produces an image nothing exercised —
    there is no shared layer cache to make it the same one, because each job runs
    its own ``docker:dind`` and every dind starts with an empty /var/lib/docker.
    """
    assert "docker build" not in script_of(pipeline, job)


@pytest.mark.parametrize("job", PUBLISH_JOBS)
def test_publish_jobs_promote_the_digest_their_build_job_recorded(pipeline, job):
    build_job, variable = PROMOTION[job]
    script = script_of(pipeline, job)

    assert "ci-image.sh promote" in script
    assert f"${{{variable}}}" in script, f"{job} must promote the digest {build_job} records"
    assert "ci-image.sh digest" in script_of(pipeline, build_job)
    assert variable in script_of(pipeline, build_job), (
        f"{build_job} must write {variable}, which {job} reads"
    )
    # `dependencies:` and not `needs:`: `needs:` would also release the publish
    # job from waiting on the rest of the pipeline, so a release could push an
    # image while pytest was still running.
    assert pipeline[job].get("dependencies") == [build_job]
    assert "needs" not in pipeline[job]


@pytest.mark.parametrize("job", BUILD_JOBS)
def test_build_jobs_carry_the_digest_forward_as_a_dotenv_report(pipeline, job):
    assert pipeline[job]["artifacts"]["reports"]["dotenv"] == "build.env"


def test_the_two_images_carry_their_digests_in_distinct_variables(pipeline):
    """Two images, two digests — and a dotenv variable is pipeline-global.

    A copy-paste that gave the platform image the session image's variable name
    would publish one image's bytes under the other's release tags while every
    other check in this file still passed, so the names being different is its
    own assertion rather than an implication of the ones above.
    """
    variables = [variable for _, variable in PROMOTION.values()]
    assert len(set(variables)) == len(variables), f"digest variables collide: {variables}"
    for job, (_, variable) in PROMOTION.items():
        others = [other for other in variables if other != variable]
        assert not any(other in script_of(pipeline, job) for other in others), (
            f"{job} reads a digest variable belonging to the other image"
        )


@pytest.mark.parametrize(
    ("dockerfile", "builder"),
    [("Containerfile", "build-container"), ("Dockerfile.platform", "build-platform-container")],
)
def test_each_image_is_built_exactly_once_in_the_whole_pipeline(pipeline, dockerfile, builder):
    """"One build per image per tag pipeline" is the other half of #189.

    Counted across every job rather than per job, because two jobs building one
    image is precisely the shape that was wrong — and a tag pipeline runs both
    stages, so any second ``-f <dockerfile>`` build is a second resolution of the
    same recipe.
    """
    builds = [
        job
        for job, definition in pipeline.items()
        if isinstance(definition, dict) and "script" in definition
        for line in definition["script"]
        if "docker build" in line and f"-f {dockerfile}" in line
    ]
    assert builds == [builder], f"{dockerfile} is built by {builds}"


def test_release_tags_are_written_only_by_the_publish_stage(pipeline):
    """The build stage pushes the commit tag; the release tags belong to publish.

    Keeping ``:${CI_COMMIT_TAG}`` and ``:latest`` out of the build stage is what
    keeps the ``environment: container-registry`` record meaningful — it is the
    job that writes the tags people pull.
    """
    for job in BUILD_JOBS:
        script = script_of(pipeline, job)
        assert "CI_COMMIT_TAG" not in script, f"{job} must not write the release tag"
        assert ":latest" not in script, f"{job} must not write :latest"


def test_the_platform_build_runs_on_merge_requests_that_change_how_it_is_invoked(pipeline):
    """Otherwise a change to this job first runs after the merge.

    The rule already carries that argument for the recipe and its COPY targets;
    the job's own invocation — build args, what it pushes, the digest it records
    — belongs in the same list, and #189 is what made that concrete. Without it,
    an edit to the build job's script rides in on a pipeline that never builds
    the image.
    """
    mr_rule = next(
        rule
        for rule in pipeline["build-platform-container"]["rules"]
        if rule.get("if") == '$CI_PIPELINE_SOURCE == "merge_request_event"'
    )
    for path in (".gitlab-ci.yml", "scripts/ci-image.sh", "Dockerfile.platform"):
        assert path in mr_rule["changes"], f"{path} must trigger the platform build on a merge request"


def test_the_promotion_script_is_executable():
    assert CI_IMAGE_SH.is_file(), f"{CI_IMAGE_SH} is missing"
    assert os.access(CI_IMAGE_SH, os.X_OK), f"{CI_IMAGE_SH} is not executable"


def test_the_promotion_script_is_posix_sh():
    """It runs in the ``docker:24.0`` job image, whose shell is BusyBox ash.

    A bashism here fails only in a tag pipeline, which is the one pipeline that
    cannot be re-run cheaply.
    """
    assert CI_IMAGE_SH.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    dash = shutil.which("dash") or shutil.which("sh")
    result = subprocess.run([dash, "-n", str(CI_IMAGE_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
