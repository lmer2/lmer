"""``Dockerfile.matrix-bridge`` and the job that builds it (issue #327).

The image is a third artifact of this repository, and the only check it has is
that CI builds it — no test can tell whether ``pip install '.[matrix]'`` still
resolves a wheel for ``python-olm``. Wave 0 of this issue shipped a
``Containerfile`` layer with a false justification precisely because no merge
request ever built that file, so the job here runs on merge requests.

What this module guards is the *hole in that guard* (!246 review): a
merge-request rule scoped by ``changes:`` only fires for the paths it lists, so
a narrow list quietly reintroduces "nobody built it" for the next person who
edits something the list does not name. The COPY sources are derived from the
Dockerfile rather than repeated here, which is what makes the list
self-maintaining: add a COPY, and this fails until the rule covers it.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DOCKERFILE = Path("Dockerfile.matrix-bridge")
JOB = "build-matrix-bridge-container"


@pytest.fixture(scope="module")
def ci():
    return yaml.safe_load(Path(".gitlab-ci.yml").read_text())


@pytest.fixture(scope="module")
def merge_request_changes(ci):
    """The ``changes:`` list of the job's merge-request rule."""
    for rule in ci[JOB]["rules"]:
        if rule.get("if", "").endswith('== "merge_request_event"'):
            return rule["changes"]
    raise AssertionError(f"{JOB} has no merge-request rule")


def copy_sources():
    """Every host path the Dockerfile copies into the image."""
    sources = []
    for line in DOCKERFILE.read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        # `COPY a b ./` — every argument but the destination is a source.
        sources.extend(line.split()[1:-1])
    return sources


def test_the_job_builds_on_merge_requests(merge_request_changes):
    """A check that first runs after the merge cannot fail the merge request
    that broke it."""
    assert merge_request_changes


def test_every_copy_source_is_in_the_merge_request_rule(merge_request_changes):
    """The guard that keeps the rule honest as the recipe changes.

    A ``COPY src/ ./src/`` means the whole package is an input to this image, so
    a change anywhere under it can break the build — and if the rule does not
    name it, the break first appears in the registry.
    """
    listed = set(merge_request_changes)
    for source in copy_sources():
        cleaned = source.rstrip("/")
        # No "a listed entry *under* this source counts" clause (!246 review).
        # It would accept `src/matrix_bridge/**/*` as covering `COPY src/`,
        # which is the exact narrowing this test exists to catch — and
        # narrowing is the likelier drift, not the rarer one.
        covered = cleaned in listed or f"{cleaned}/**/*" in listed
        assert covered, (
            f"Dockerfile.matrix-bridge copies {source!r}, and the job's "
            f"merge-request rule does not list it: {sorted(listed)}"
        )


def test_the_recipe_and_the_job_that_invokes_it_are_both_inputs(
    merge_request_changes,
):
    """`build-platform-container`'s argument, for the same reason: a change to
    how the job invokes the recipe would otherwise first run after the merge."""
    assert "Dockerfile.matrix-bridge" in merge_request_changes
    assert ".gitlab-ci.yml" in merge_request_changes


def test_the_image_installs_the_matrix_extra():
    """The bridge's dependencies are optional for everyone else, so an install
    without the extra produces an image whose entrypoint cannot import."""
    assert "'.[matrix]'" in DOCKERFILE.read_text()


def test_the_entrypoint_is_the_console_script():
    text = DOCKERFILE.read_text()
    assert 'ENTRYPOINT ["lmer-matrix-bridge"]' in text
    assert 'CMD ["run"]' in text


def test_nothing_claims_an_update_mechanism_that_does_not_exist():
    """No publish job promotes this image to a moving tag, so a documented
    ``AutoUpdate=registry`` would name an update path with nothing behind it —
    the same shape as an `experimental_features` key Synapse never reads
    (!246 review). Remove this test when a publish job exists.
    """
    ci_text = Path(".gitlab-ci.yml").read_text()
    docs = Path("docs/MATRIX-CHAT.md").read_text()

    publishes = "publish-matrix-bridge-container" in ci_text
    unit_block = docs.split("[Container]")[1].split("[Service]")[0]
    # A *directive*, not a mention: the unit's own comment explains why the
    # directive is absent, and a substring match would read that explanation as
    # the thing it warns against.
    directives = [
        line for line in unit_block.splitlines()
        if line.strip().startswith("AutoUpdate=")
    ]
    assert publishes or not directives, (
        f"the documented unit sets {directives} with no publish job to feed it"
    )
