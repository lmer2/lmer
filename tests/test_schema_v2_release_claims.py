"""Separate execution records still arbitrate one shared project release."""

import pytest
import subprocess
import yaml

from tests.test_schema_v2_sessions import session_tree, seed_launch  # noqa: F401
from work_repo import run_state, session_record


def test_two_session_records_cannot_claim_the_same_release(session_tree):
    from work_repo import release_claims

    first, first_dir, _ = seed_launch()
    release_claims.change_claim(first["uid"])
    second, second_dir, _ = seed_launch()
    assert first_dir != second_dir
    with pytest.raises(run_state.RunStateError, match="held"):
        release_claims.change_claim(second["uid"])
    assert release_claims.read_claim()["holder"] == first["uid"]


def test_old_cleanup_cannot_release_successors_claim(session_tree):
    from work_repo import release_claims

    first = session_record.start_launch()["uid"]
    release_claims.change_claim(first)
    release_claims.change_claim(first, release=True)
    second = session_record.start_launch()["uid"]
    release_claims.change_claim(second)
    with pytest.raises(run_state.RunStateError, match="held"):
        release_claims.change_claim(first, release=True)
    assert release_claims.read_claim()["holder"] == second


def test_stale_timestamp_is_not_automatic_takeover_permission(session_tree):
    from work_repo import release_claims

    first = session_record.start_launch()["uid"]
    release_claims.change_claim(first)
    path = release_claims.claim_path()
    path.write_text(f'schema: 1\nholder: {first}\nupdated: "1900-01-01T00:00:00Z"\n')
    with pytest.raises(run_state.RunStateError, match="held"):
        release_claims.change_claim(session_record.start_launch()["uid"])


def test_release_file_cannot_be_changed_by_foreign_session(session_tree):
    from work_repo import release_run

    _, directory, _ = seed_launch()
    session_record.start_launch()
    with pytest.raises(run_state.RunStateError, match="own"):
        release_run.write_release(directory, {"schema": 1, "version": "1.0.0"})
    assert not (directory / "release.yaml").exists()


def test_foreign_record_rename_refuses_before_moving_it(session_tree):
    _, source, state = seed_launch()
    state["name"] = "new-name"
    session_record.start_launch()
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.freeze_run_dir(source, state)
    assert source.is_dir()


def git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True,
        capture_output=True, text=True, timeout=30,
    ).stdout


@pytest.fixture
def claim_clones(session_tree, tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    git(session_tree, "init", "--initial-branch=main")
    git(session_tree, "config", "user.name", "Test")
    git(session_tree, "config", "user.email", "test@example.test")
    git(session_tree, "add", "_plexus.yaml")
    git(session_tree, "commit", "-m", "schema fixture")
    git(session_tree, "remote", "add", "origin", str(origin))
    git(session_tree, "push", "-u", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    git(other, "config", "user.name", "Test")
    git(other, "config", "user.email", "test@example.test")
    return origin, session_tree, other


def _published_claim(origin):
    """The claim as the REMOTE holds it — where a shared claim actually lives.

    The claim verb publishes an object and never writes the checkout, so the
    worktree's copy is only as fresh as the last pull; asserting on it would
    be asserting on a cache.
    """
    return yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))


def test_claim_cli_publishes_shared_claim_and_refuses_second_writer(claim_clones, monkeypatch):
    from work_repo import cli

    origin, root, _ = claim_clones
    published = []
    real_publish = cli.publish_overlay_commit

    def publish(repo_path, **kwargs):
        published.append(kwargs["blobs"])
        return real_publish(repo_path, **kwargs)

    monkeypatch.setattr(cli, "publish_overlay_commit", publish)
    first, _, _ = seed_launch()
    assert cli.cmd_release_claim() == 0
    assert _published_claim(origin)["holder"] == first["uid"]
    assert "widgets/_claims/release.yaml" in published[-1]
    seed_launch()
    assert cli.cmd_release_claim() == 1
    assert len(published) == 1, "a refused claim must not publish anything"
    assert cli.cmd_release_unclaim() == 1
    assert cli.cmd_release_unclaim(force=True) == 0
    assert _published_claim(origin)["holder"] is None
    # `claim-status` reads the last fetched publication, so it is current
    # even though the claim verb never wrote the checkout.
    assert cli._last_published_claim()["holder"] is None
    assert not git(root, "status", "--porcelain"), "a claim must leave the checkout clean"


def test_real_git_race_has_one_winner_across_distinct_records(claim_clones, monkeypatch):
    from work_repo import cli, git_ops

    origin, first_clone, second_clone = claim_clones
    first = session_record.start_launch()
    real_push = git_ops.publish_overlay_commit
    raced = []

    def push_with_race(root, **kwargs):
        if not raced:
            raced.append(True)
            with monkeypatch.context() as child:
                child.setenv("LMER_WORK_REPO_PATH", str(second_clone))
                child.setenv("LMER_SESSION_METADATA", "{}")
                child.setenv("LMER_SESSION_ID", "")
                second = session_record.start_launch()
                assert cli.cmd_release_claim() == 0
                raced.append(second["uid"])
        return real_push(root, **kwargs)

    monkeypatch.setattr(cli, "publish_overlay_commit", push_with_race)
    assert cli.cmd_release_claim() == 1
    remote = yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))
    assert remote["holder"] == raced[1]
    assert remote["holder"] != first["uid"]


def test_a_failed_publication_leaves_the_checkout_and_the_remote_untouched(
    claim_clones, monkeypatch
):
    """The claim never enters the checkout's index, so a failure has nothing
    to leak into a later push and nothing to roll back."""
    from work_repo import cli, git_ops, release_claims

    origin, root, _ = claim_clones
    seed_launch()
    before_head = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(cli, "publish_overlay_commit", lambda *a, **k: (
        git_ops.CLAIM_PUSH_ERROR, "simulated publication failure", ""))
    assert cli.cmd_release_claim() == 1
    assert release_claims.read_claim()["holder"] is None
    assert not (root / release_claims.claim_rel_path()).exists()
    assert not git(root, "diff", "--cached")
    assert not git(root, "status", "--porcelain")
    # The local snapshot of this session's own record is the only local commit
    # a claim makes, and it carries no claim: there is no claim commit to drop.
    assert git(root, "rev-parse", "HEAD") != before_head
    assert "local snapshot" in git(root, "log", "--format=%s", "-1")
    assert release_claims.claim_rel_path() not in git(
        root, "show", "--name-only", "--format=", "HEAD"
    )
    published = subprocess.run(
        ["git", "-C", str(origin), "show", "main:widgets/_claims/release.yaml"],
        capture_output=True, text=True,
    )
    assert published.returncode != 0, "nothing may be published when the publication fails"


def test_the_publication_index_never_becomes_the_checkouts_index(claim_clones):
    """The temporary index lives outside every commit path set and is removed."""
    from work_repo import cli

    _origin, root, _ = claim_clones
    seed_launch()
    (root / "unrelated.txt").write_text("staged by someone else\n")
    git(root, "add", "unrelated.txt")
    staged_before = git(root, "diff", "--cached", "--name-only")
    assert cli.cmd_release_claim() == 0
    assert git(root, "diff", "--cached", "--name-only") == staged_before
    assert not list((root / ".git").glob("lmer-publish-index.*"))
