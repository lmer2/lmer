"""Tests for .github/scripts/verify-tag-signature.sh (release publish gate).

The script is the fail-closed gate between a tag push and PyPI publish: it
must accept only an SSH-signed tag whose signer is in the admin-controlled
RELEASE_ALLOWED_SIGNERS allowlist AND whose commit equals GitHub main HEAD.
Everything here is hermetic — throwaway ed25519 keys are generated per test
session, fixture repos live in tmp_path, and the GitHub API is stubbed via
the script's GITHUB_API_URL seam with a file:// tree (no network, no token).

A tag push runs the release workflow from the tag's own tree, so the one
property these tests guard hardest is that nothing inside the checkout
(planted allowed_signers files, repo-local git config) can influence the
verification result.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "verify-tag-signature.sh"

REPO_SLUG = "acme/lmer"
ALLOWED_PRINCIPAL = "release@example.com"
ROGUE_PRINCIPAL = "rogue@example.com"


def _git_ssh_signing_available():
    """SSH signature signing/verification needs git >= 2.34."""
    if shutil.which("git") is None:
        return False
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= (2, 34)


pytestmark = [
    pytest.mark.skipif(
        shutil.which("ssh-keygen") is None, reason="ssh-keygen not available"
    ),
    pytest.mark.skipif(
        not _git_ssh_signing_available(), reason="git >= 2.34 (ssh signing) not available"
    ),
    pytest.mark.skipif(shutil.which("jq") is None, reason="jq not available"),
    pytest.mark.skipif(shutil.which("curl") is None, reason="curl not available"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available"),
]


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    """Two throwaway ed25519 keypairs: one allowlisted, one rogue."""
    keydir = tmp_path_factory.mktemp("keys")
    out = {}
    for name, comment in (("allowed", ALLOWED_PRINCIPAL), ("rogue", ROGUE_PRINCIPAL)):
        priv = keydir / f"{name}_key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment,
             "-f", str(priv)],
            check=True, capture_output=True,
        )
        # "key-type base64-key" — the pub file minus its trailing comment.
        pub = " ".join((priv.with_suffix(".pub")).read_text().split()[:2])
        out[name] = {"private": priv, "pub": pub}
    return out


def _allowed_signers_line(keys, name, principal=ALLOWED_PRINCIPAL):
    return f"{principal} {keys[name]['pub']}"


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", f"user.email={ALLOWED_PRINCIPAL}",
         *args],
        check=True, capture_output=True,
    )


def _make_repo(tmp_path):
    """Fixture repo with one commit; returns (repo_path, head_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("fixture\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return repo, head


def _sign_tag(repo, tag, private_key):
    """Create an SSH-signed annotated tag (git tag -s under gpg.format=ssh)."""
    _git(
        repo,
        "-c", "gpg.format=ssh",
        "-c", f"user.signingkey={private_key}",
        "tag", "-s", tag, "-m", f"release {tag}",
    )


def _api_stub(tmp_path, sha, slug=REPO_SLUG, body=None):
    """file:// tree serving repos/<slug>/commits/heads/main; returns API base."""
    heads = tmp_path / "api" / "repos" / slug / "commits" / "heads"
    heads.mkdir(parents=True, exist_ok=True)
    (heads / "main").write_text(body if body is not None else f'{{"sha": "{sha}"}}\n')
    return f"file://{tmp_path}/api"


def _run_script(repo, tmp_path, **env_overrides):
    """Run the script in the fixture repo with a clean, hermetic environment.

    Only PATH leaks in from the host; git config is pinned away from the
    developer's global/system files. Seams are passed explicitly — a key
    absent from env_overrides is absent from the environment (unset), and a
    None value also means unset.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(repo), env=env, capture_output=True, text=True,
    )


class TestVerifyTagSignatureScript:
    def test_script_exists_and_is_executable(self):
        assert SCRIPT.is_file()
        assert os.access(SCRIPT, os.X_OK)

    def test_pass_signed_by_allowed_key_matching_main_head(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_SHA=head,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode == 0, result.stderr
        assert "Tag 'v1.0.0' verified" in result.stdout
        assert head in result.stdout

    def test_fails_when_allowed_signers_empty(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS="",
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::RELEASE_ALLOWED_SIGNERS is empty or unset" in result.stderr

    def test_fails_when_allowed_signers_unset(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        result = _run_script(
            repo, tmp_path,
            # RELEASE_ALLOWED_SIGNERS deliberately absent from the environment.
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::RELEASE_ALLOWED_SIGNERS is empty or unset" in result.stderr

    def test_fails_when_ref_name_unset(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::GITHUB_REF_NAME is empty or unset" in result.stderr

    def test_fails_when_repository_unset(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::GITHUB_REPOSITORY is empty or unset" in result.stderr

    def test_fails_when_tag_missing_from_checkout(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v9.9.9",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::tag 'v9.9.9' not found in the checkout" in result.stderr

    def test_fails_for_signer_not_in_allowlist(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["rogue"]["private"])
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::signature verification failed for tag 'v1.0.0'" in result.stderr

    def test_fails_for_unsigned_annotated_tag(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _git(repo, "tag", "-a", "v1.0.0", "-m", "release v1.0.0")
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::signature verification failed for tag 'v1.0.0'" in result.stderr

    def test_fails_for_lightweight_tag(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _git(repo, "tag", "v1.0.0")
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::signature verification failed for tag 'v1.0.0'" in result.stderr

    def test_fails_when_api_unreachable(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        # No stub tree at all: the curl file:// fetch fails, which stands in
        # for any API failure (network error, 404, 5xx under --fail).
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_SHA=head,
            GITHUB_API_URL=f"file://{tmp_path}/no-such-api",
        )
        assert result.returncode != 0
        assert "::error::failed to resolve GitHub main HEAD from" in result.stderr

    def test_fails_when_api_response_lacks_sha(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_SHA=head,
            GITHUB_API_URL=_api_stub(tmp_path, None, body='{"message": "moved"}\n'),
        )
        assert result.returncode != 0
        assert "has no .sha field" in result.stderr

    def test_fails_when_tag_commit_is_not_main_head(self, tmp_path, keys):
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        stale = "0" * 40
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_SHA=head,
            GITHUB_API_URL=_api_stub(tmp_path, stale),
        )
        assert result.returncode != 0
        assert "refusing to publish" in result.stderr
        assert stale in result.stderr

    def test_fails_when_github_sha_is_unset(self, tmp_path, keys):
        """The pin cannot be skipped by simply not providing the variable."""
        repo, _ = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, "0" * 40),
        )
        assert result.returncode != 0
        assert "::error::GITHUB_SHA is empty or unset" in result.stderr

    def test_fails_when_the_tag_moved_after_the_push_event(self, tmp_path, keys):
        """What is VERIFIED must be what gets PUBLISHED.

        build/publish check out ${GITHUB_SHA} — the commit the tag resolved
        to when the push event fired — while this job re-reads the tag at
        its own runtime. A tag re-pointed in between (tag-write on the
        mirror is an accepted PAT residual) would otherwise let this job
        verify one commit while another is published."""
        repo, head = _make_repo(tmp_path)
        _sign_tag(repo, "v1.0.0", keys["allowed"]["private"])
        moved_from = "0" * 40
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_SHA=moved_from,
            # main HEAD agrees with the tag, so ONLY the trigger-sha pin
            # can be what refuses here.
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "the tag moved after the push event" in result.stderr
        assert moved_from in result.stderr
        assert head in result.stderr

    def test_decoy_allowed_signers_in_checkout_is_ignored(self, tmp_path, keys):
        """Tag-borne repo content must never become trust-anchor material.

        A rogue tagger who controls the tag's tree can plant allowed_signers
        files allowlisting their own key — and even set repo-local git config
        pointing at one. Verification must still fail because the script only
        trusts RELEASE_ALLOWED_SIGNERS (which lists the real key alone).
        """
        repo, head = _make_repo(tmp_path)
        decoy = _allowed_signers_line(keys, "rogue", principal=ROGUE_PRINCIPAL)
        (repo / "allowed_signers").write_text(decoy + "\n")
        (repo / ".github").mkdir()
        (repo / ".github" / "allowed_signers").write_text(decoy + "\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "plant decoy allowed_signers")
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Repo-local config is attacker-influenced too on a hostile runner;
        # the script's command-line -c must take precedence over it.
        _git(repo, "config", "gpg.ssh.allowedSignersFile",
             str(repo / "allowed_signers"))
        _sign_tag(repo, "v1.0.0", keys["rogue"]["private"])
        result = _run_script(
            repo, tmp_path,
            RELEASE_ALLOWED_SIGNERS=_allowed_signers_line(keys, "allowed"),
            GITHUB_REF_NAME="v1.0.0",
            GITHUB_REPOSITORY=REPO_SLUG,
            GITHUB_API_URL=_api_stub(tmp_path, head),
        )
        assert result.returncode != 0
        assert "::error::signature verification failed for tag 'v1.0.0'" in result.stderr
        # git may still note the signature is cryptographically well-formed
        # ('Good "git" signature with ... No principal matched.'), but the
        # acceptance form — 'Good "git" signature for <principal>' — must
        # never appear: no principal from the decoy files may match.
        combined = result.stdout + result.stderr
        assert 'Good "git" signature for' not in combined
