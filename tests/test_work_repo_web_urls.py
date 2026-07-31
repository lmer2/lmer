"""Tests for web URL rendering of run artifacts (issue #104).

`git_ops.web_url_for` maps a path inside the work-repo checkout to its
GitLab blob/tree URL, derived from the checkout's `origin` remote with
credentials STRIPPED — the tokenized remote the runner clones with must
never leak into user-facing output — and the CLI wires it into every
user-facing artifact print (artifact, report, log display, resume brief).

Since T66 the path-shape half of that is extracted (`detect_forge`,
`forge_web_url`) so the platform's file listing can build the same URLs from
its own checkout instead of keeping a second copy — a copy is how one forge
keeps a bug the other fixed. Two properties of the extraction are pinned
below: GitHub gets `/blob/` where GitLab gets `/-/blob/`, and a caller that asks
for neither a forge nor a default gets NO url rather than a guessed one. Both
link-building callers do pass GitLab as that default — `web_url_for` since #104
and the platform's run-file listing since T86 — because a self-hosted GitLab is
usually at `git.<domain>`, and the two surfaces answering differently for the
same run dir was the bug T86 fixed. The `forge=` override they carry for the
platform's `work_repo_forge` knob is pinned here too.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import git_ops, run_state
from tests.conftest import strip_lmer_env

TOKEN = "glpat-supersecrettoken12345678"
TOKENIZED_REMOTE = f"https://oauth2:{TOKEN}@gitlab.example.com/group/work.git"
WEB_BASE = "https://gitlab.example.com/group/work"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_work_repo(
    path: Path, remote: str | None = TOKENIZED_REMOTE, branch: str = "main"
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


class TestWebUrlFor:
    """URL derivation kernel — git_ops.web_url_for."""

    def test_https_token_remote_file_blob_url(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "a" / "b.md"
        target.parent.mkdir()
        target.write_text("x")
        url = git_ops.web_url_for(target)
        assert url == f"{WEB_BASE}/-/blob/main/a/b.md"

    def test_token_stripped_from_url(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "f.md"
        target.write_text("x")
        url = git_ops.web_url_for(target)
        assert url is not None
        assert TOKEN not in url
        assert "oauth2" not in url

    def test_dir_gets_tree_url(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "runs" / "develop-issue-1"
        target.mkdir(parents=True)
        url = git_ops.web_url_for(target)
        assert url == f"{WEB_BASE}/-/tree/main/runs/develop-issue-1"

    def test_repo_root_gets_tree_url(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        assert git_ops.web_url_for(repo) == f"{WEB_BASE}/-/tree/main"

    def test_scp_style_ssh_remote(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path, remote="git@gitlab.example.com:group/work.git")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "f.md"
        target.write_text("x")
        assert git_ops.web_url_for(target) == f"{WEB_BASE}/-/blob/main/f.md"

    def test_ssh_scheme_remote(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path, remote="ssh://git@gitlab.example.com/group/work.git")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "f.md"
        target.write_text("x")
        assert git_ops.web_url_for(target) == f"{WEB_BASE}/-/blob/main/f.md"

    def test_no_remote_returns_none(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path, remote=None)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "f.md"
        target.write_text("x")
        assert git_ops.web_url_for(target) is None

    def test_not_a_git_repo_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        target = tmp_path / "f.md"
        target.write_text("x")
        assert git_ops.web_url_for(target) is None

    def test_missing_work_repo_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "absent"))
        assert git_ops.web_url_for(tmp_path / "absent" / "f.md") is None

    def test_path_outside_repo_returns_none(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path / "work")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        outside = tmp_path / "elsewhere.md"
        outside.write_text("x")
        assert git_ops.web_url_for(outside) is None

    def test_missing_path_returns_none(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        assert git_ops.web_url_for(repo / "no-such-file.md") is None

    def test_branch_detected_from_checkout(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path, branch="work-main")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "f.md"
        target.write_text("x")
        assert git_ops.web_url_for(target) == f"{WEB_BASE}/-/blob/work-main/f.md"

    def test_detached_head_defaults_to_main(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path, branch="work-main")
        target = repo / "f.md"
        target.write_text("x")
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c", "user.email=t@example.com",
            "-c", "user.name=t",
            "commit", "-m", "seed",
        )
        _git(repo, "checkout", "--detach")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        assert git_ops.web_url_for(target) == f"{WEB_BASE}/-/blob/main/f.md"

    def test_relpath_is_url_quoted(self, tmp_path, monkeypatch):
        repo = _init_work_repo(tmp_path)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "a file.md"
        target.write_text("x")
        assert git_ops.web_url_for(target) == f"{WEB_BASE}/-/blob/main/a%20file.md"


class TestForgeDetection:
    """Which forge a hostname is — the rule the path shape is chosen by."""

    @pytest.mark.parametrize(
        "host",
        ["github.com", "GITHUB.COM", "gist.github.com", "acme.ghe.com"],
    )
    def test_github_hosts(self, host):
        assert git_ops.detect_forge(host) == git_ops.FORGE_GITHUB

    @pytest.mark.parametrize(
        "host",
        ["gitlab.com", "gitlab.example.com", "gitlab-ce.example.com",
         "salsa.gitlab.com"],
    )
    def test_gitlab_hosts(self, host):
        assert git_ops.detect_forge(host) == git_ops.FORGE_GITLAB

    @pytest.mark.parametrize(
        "host", ["git.example.com", "code.internal", "bitbucket.org", "", None],
    )
    def test_unrecognised_hosts_are_none(self, host):
        """No guessing: an unknown host's path layout is unknown."""
        assert git_ops.detect_forge(host) is None

    def test_a_port_is_ignored(self):
        """Callers pass a base URL's authority straight in."""
        assert git_ops.detect_forge("gitlab.example.com:8443") == git_ops.FORGE_GITLAB

    def test_the_github_rule_is_the_token_lookups_rule(self):
        """One classification, not two: a host that is GitHub for credentials and
        something else for URLs is a bug with no symptom until someone clicks."""
        from lmer_cli import tokens

        for host in ("github.com", "acme.ghe.com", "git.example.com"):
            assert (git_ops.detect_forge(host) == git_ops.FORGE_GITHUB) is (
                tokens._is_github_host(host)
            )


class TestForgeWebUrl:
    """The extracted path shapes — the one definition of blob-vs-tree per forge."""

    def test_gitlab_file(self):
        assert git_ops.forge_web_url(
            "https://gitlab.example.com/group/work", "main", "runs/r/spec.md"
        ) == "https://gitlab.example.com/group/work/-/blob/main/runs/r/spec.md"

    def test_github_file_has_no_dash_segment(self):
        assert git_ops.forge_web_url(
            "https://github.com/owner/work", "main", "runs/r/spec.md"
        ) == "https://github.com/owner/work/blob/main/runs/r/spec.md"

    def test_github_dir(self):
        assert git_ops.forge_web_url(
            "https://github.com/owner/work", "main", "runs/r", is_dir=True
        ) == "https://github.com/owner/work/tree/main/runs/r"

    def test_repo_root_is_a_tree(self):
        assert git_ops.forge_web_url(
            "https://github.com/owner/work", "trunk"
        ) == "https://github.com/owner/work/tree/trunk"

    def test_unrecognised_forge_gets_no_url(self):
        """A broken link is worse than a plain filename — 'etc.' in the ask is not
        licence to invent path layouts."""
        assert git_ops.forge_web_url(
            "https://git.example.com/group/work", "main", "runs/r/spec.md"
        ) is None

    def test_unrecognised_forge_takes_a_default_when_one_is_given(self):
        """What both link-building surfaces pass: a work repo on a plain hostname is
        a self-hosted GitLab in practice (`web_url_for`, and the platform's run-file
        listing since T86)."""
        assert git_ops.forge_web_url(
            "https://git.example.com/group/work", "main", "spec.md",
            default_forge=git_ops.FORGE_GITLAB,
        ) == "https://git.example.com/group/work/-/blob/main/spec.md"

    def test_an_explicit_forge_beats_detection(self):
        """The operator's override (the platform's `work_repo_forge`) answers a
        question the hostname cannot, so detection must not win over it — in either
        direction, since a GitHub Enterprise Server can be called anything."""
        assert git_ops.forge_web_url(
            "https://gitlab.example.com/group/work", "main", "spec.md",
            forge=git_ops.FORGE_GITHUB,
        ) == "https://gitlab.example.com/group/work/blob/main/spec.md"
        assert git_ops.forge_web_url(
            "https://github.com/owner/work", "main", "spec.md",
            forge=git_ops.FORGE_GITLAB,
        ) == "https://github.com/owner/work/-/blob/main/spec.md"

    def test_an_explicit_forge_beats_the_default_too(self):
        assert git_ops.forge_web_url(
            "https://git.example.com/group/work", "main", "spec.md",
            forge=git_ops.FORGE_GITHUB, default_forge=git_ops.FORGE_GITLAB,
        ) == "https://git.example.com/group/work/blob/main/spec.md"

    def test_a_forge_that_is_no_forge_switches_the_link_off(self):
        """How `work_repo_forge=none` reaches here: a value with no path shape gets
        no URL, and the default cannot resurrect it."""
        assert git_ops.forge_web_url(
            "https://gitlab.example.com/group/work", "main", "spec.md",
            forge="none", default_forge=git_ops.FORGE_GITLAB,
        ) is None

    def test_no_base_gets_no_url(self):
        assert git_ops.forge_web_url("", "main", "spec.md") is None

    def test_ref_and_path_are_quoted(self):
        assert git_ops.forge_web_url(
            "https://github.com/owner/work", "fix/a b", "a file.md"
        ) == "https://github.com/owner/work/blob/fix/a%20b/a%20file.md"


class TestWebUrlForAcrossForges:
    """`web_url_for` keeps every existing caller's URL and gains GitHub."""

    def test_github_work_repo_gets_githubs_shape(self, tmp_path, monkeypatch):
        repo = _init_work_repo(
            tmp_path, remote=f"https://x-access-token:{TOKEN}@github.com/owner/work.git"
        )
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "f.md"
        target.write_text("x")
        url = git_ops.web_url_for(target)
        assert url == "https://github.com/owner/work/blob/main/f.md"
        assert TOKEN not in url

    def test_a_self_hosted_gitlab_on_a_plain_hostname_keeps_its_links(
        self, tmp_path, monkeypatch
    ):
        """The regression this default exists to prevent: `git.<domain>` is what a
        self-hosted GitLab is usually called, and its links have worked since #104."""
        repo = _init_work_repo(
            tmp_path, remote="https://git.example.com/group/work.git"
        )
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        target = repo / "f.md"
        target.write_text("x")
        assert git_ops.web_url_for(target) == (
            "https://git.example.com/group/work/-/blob/main/f.md"
        )


class TestCheckoutBranch:
    def test_reads_the_checkouts_branch(self, tmp_path):
        repo = _init_work_repo(tmp_path / "r", branch="work-main")
        assert git_ops.checkout_branch(repo) == "work-main"

    def test_a_directory_that_is_not_a_repo_falls_back(self, tmp_path):
        assert git_ops.checkout_branch(tmp_path) == git_ops.DEFAULT_WEB_BRANCH

    def test_a_missing_directory_falls_back_rather_than_raising(self, tmp_path):
        assert git_ops.checkout_branch(tmp_path / "absent") == (
            git_ops.DEFAULT_WEB_BRANCH
        )


class TestWebBaseFromRemote:
    def test_local_path_remote_returns_none(self):
        assert git_ops._web_base_from_remote("/srv/git/work.git") is None
        assert git_ops._web_base_from_remote("../work") is None

    def test_https_port_preserved(self):
        base = git_ops._web_base_from_remote("https://user:pw@git.example.com:8443/g/p.git")
        assert base == "https://git.example.com:8443/g/p"


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """A git work-repo checkout with a tokenized origin remote, plus the
    env trio the run-state layer resolves paths from. Returns the run dir."""
    _init_work_repo(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "issue-123")
    monkeypatch.setenv("LMER_SESSION_ID", "s-web-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-123"


RUN_REL = "git.example.com/org/repo/runs/develop-issue-123"


class TestCliOutputsCarryWebUrls:
    """The user-facing prints (issue #104) — URL present, token never."""

    def test_artifact_prints_blob_url(self, run_env, tmp_path, capsys):
        src = tmp_path / "summary.md"
        src.write_text("# agreed\n")
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["artifact", "spec.md", "--file", str(src)]) == 0
        out = capsys.readouterr().out
        assert f"{WEB_BASE}/-/blob/main/{RUN_REL}/spec.md" in out
        assert TOKEN not in out

    def test_report_prints_blob_url(self, run_env, tmp_path, capsys):
        src = tmp_path / "report.md"
        src.write_text("# findings\n")
        assert _main(["report", "--file", str(src)]) == 0
        out = capsys.readouterr().out
        assert f"{WEB_BASE}/-/blob/main/{RUN_REL}/reports/" in out
        assert TOKEN not in out

    def test_resume_brief_ends_with_run_dir_tree_url(self, run_env, capsys):
        run_state.write_state(
            run_env, run_state.seed_state("develop-issue-123", "develop", "t")
        )
        assert _main(["resume"]) == 0
        out = capsys.readouterr().out
        assert out.rstrip().endswith(f"Run dir: {WEB_BASE}/-/tree/main/{RUN_REL}")
        assert TOKEN not in out

    def test_session_start_brief_carries_run_dir_url(self, run_env, capsys):
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert f"Run dir: {WEB_BASE}/-/tree/main/{RUN_REL}" in out
        assert TOKEN not in out

    def test_log_display_prints_blob_url(self, run_env, capsys):
        run_env.mkdir(parents=True)
        (run_env / "log.yaml").write_text("- message: hello\n")
        assert _main(["log"]) == 0
        out = capsys.readouterr().out
        assert f"Web: {WEB_BASE}/-/blob/main/{RUN_REL}/log.yaml" in out
        assert TOKEN not in out

    def test_no_remote_falls_back_to_plain_paths(self, run_env, tmp_path, capsys):
        _git(tmp_path, "remote", "remove", "origin")
        src = tmp_path / "summary.md"
        src.write_text("# agreed\n")
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["artifact", "spec.md", "--file", str(src)]) == 0
        run_state.append_event(run_env, "phase", note="interview")
        assert _main(["resume"]) == 0
        out = capsys.readouterr().out
        assert "Web:" not in out
        assert "Run dir:" not in out
        assert str(run_env / "spec.md") in out  # plain path, as before

    def test_resume_json_mode_unchanged(self, run_env, capsys):
        run_state.write_state(
            run_env, run_state.seed_state("develop-issue-123", "develop", "t")
        )
        assert _main(["resume", "--json"]) == 0
        out = capsys.readouterr().out
        assert "Run dir:" not in out
        assert TOKEN not in out

    def test_resume_json_carries_run_dir_url(self, run_env, capsys):
        # Issue #100: the guard's push nudge gets its clickable link from
        # here — the URL is derived server-side, credentials stripped.
        run_state.write_state(
            run_env, run_state.seed_state("develop-issue-123", "develop", "t")
        )
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["run_dir_url"] == f"{WEB_BASE}/-/tree/main/{RUN_REL}"

    def test_resume_json_omits_run_dir_url_without_remote(self, run_env, tmp_path, capsys):
        _git(tmp_path, "remote", "remove", "origin")
        run_state.write_state(
            run_env, run_state.seed_state("develop-issue-123", "develop", "t")
        )
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert "run_dir_url" not in decision
        assert decision["run_dir"] == str(run_env)  # the path field stays
