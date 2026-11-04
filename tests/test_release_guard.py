"""Tests for ``lmer-release-guard`` (src/lmer_cli/release_guard.py).

Covers the detectors and masking, the diff/commit/merge/message scans, the
project-tier ``release.leak_scan`` loader, and the pre-push guard's ref,
ancestry, guard-string and live-secret checks. Every repository is a
temporary one built here; every fixture value is neutral and fake.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from lmer_cli import release_guard as rg
from work_repo import utils as work_utils

# Assembled at run time so no credential-shaped literal sits in this file.
FAKE_PAT = "gl" + "pat-" + "Q" * 24
FAKE_LIVE = "live-" + "fixture-" + "value-0123456789"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No real session secret or git config reaches a test."""
    for name in list(os.environ):
        if work_utils._SECRET_NAME_PATTERNS.search(name):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var, value in {
        "GIT_AUTHOR_NAME": "Fixture Author",
        "GIT_AUTHOR_EMAIL": "author@example.org",
        "GIT_COMMITTER_NAME": "Fixture Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.org",
    }.items():
        monkeypatch.setenv(var, value)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo, files, message="change"):
    for name, text in files.items():
        path = repo / name
        if text is None:
            git(repo, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, {"README.md": "hello\n"}, "base")
    return path


def write_config(tmp_path, block="public_mirror: true\n", name="task.yaml"):
    path = tmp_path / name
    leak_scan = "" if block is None else "  leak_scan:\n" + "".join(
        f"    {line}\n" for line in block.splitlines())
    path.write_text("release:\n  github_target: https://example.invalid/mirror\n" + leak_scan)
    return path


def scanner(config=None, secrets=()):
    return rg.Scanner(config or rg.LeakScanConfig(public_mirror=True), list(secrets))


def cats(result):
    return {hit["cat"] for hit in result["hits"]}


class TestDetectors:
    @pytest.mark.parametrize("cat, text", [
        ("token-prefix", f"value {FAKE_PAT}"),
        ("token-shape", "AKIA" + "Z" * 16),
        ("url-userinfo", "https://user:pw@example.invalid/x"),
        ("secret-assign", "pass" + "word = 'hunter-two'"),
        ("aws-secret", "aws_" + "secret_access_key: abc"),
        ("openssh-body", "b3BlbnNzaC1rZXktdjE" + "AAAA"),
        ("slack-webhook", "https://hooks.slack.com/services/T0/B0/xyz"),
        ("google-api", "AIza" + "a" * 35),
        ("jwt", "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + ".sig"),
        ("stripe", "sk_" + "live_" + "a" * 12),
        ("npm", "npm_" + "a" * 36),
        ("pypi", "pypi-AgE" + "a" * 24),
        ("matrix", "syt_" + "a" * 20),
        ("env-secret-assign", "export DEPLOY_" + "TOKEN=abcdefgh"),
        ("yaml-json-secret", '"client_' + 'secret": "abcdefgh"'),
        ("auth-header", "Authorization: Bearer " + "a" * 16),
        ("email", "mail someone@example.org"),
        ("phone", "call 555 123 4567"),
        ("internal-host", "ssh build.corp"),
        ("rfc1918", "connect 10.1.2.3"),
        ("home-path", "/home/someone/x"),
    ])
    def test_each_generic_category_fires(self, cat, text):
        assert cat in cats(scanner().scan([("f", 1, text)]))

    def test_plain_prose_is_clean(self):
        result = scanner().scan([("f", 1, "the release pushes main first, then the tag")])
        assert result["hits"] == []

    def test_project_patterns_add_categories(self):
        config = rg.parse_leak_scan(
            {"public_mirror": True, "patterns": [{"name": "codename", "regex": r"(?i)\bbluefin\b"}]}, "t")
        assert "codename" in cats(scanner(config).scan([("f", 1, "the Bluefin host")]))
        assert "codename" not in cats(scanner().scan([("f", 1, "the Bluefin host")]))

    def test_no_project_private_detector_ships(self):
        """The shipped set is the generic one only; private names come from config."""
        names = {name for name, _ in rg.GENERIC_PATTERNS}
        assert "handle" not in names
        assert not any(name.startswith("host-") for name in names)


class TestMasking:
    def test_mask_keeps_only_edges_and_length(self):
        assert rg.mask("abcdefghij") == "abc…ij(10)"
        assert rg.mask("abc") == "a…(3)"

    def test_hits_carry_no_full_value(self):
        result = scanner().scan([("f", 1, f"token {FAKE_PAT}")])
        assert FAKE_PAT not in json.dumps(result)

    def test_live_secret_shows_no_characters(self):
        result = scanner(secrets=[FAKE_LIVE]).scan([("f", 3, f"x {FAKE_LIVE} y")])
        live = [h for h in result["hits"] if h["cat"] == rg.LIVE_CATEGORY]
        assert live == [{"file": "f", "line": 3, "cat": rg.LIVE_CATEGORY,
                         "masked": f"live secret #1 ({len(FAKE_LIVE)})"}]


class TestLiveSecrets:
    def test_env_credential_store_and_local(self, monkeypatch, tmp_path, repo):
        monkeypatch.setenv("FIXTURE_TOKEN", "env-value-12345")
        home = Path(os.environ["HOME"])
        (home / ".git-credentials").write_text("https://user:store-value-12345@example.invalid\n")
        (repo / "LOCAL").mkdir()
        (repo / "LOCAL" / "env").write_text("SOMETHING=local-value-12345\n")
        values = rg.live_secrets(repo)
        assert {"env-value-12345", "store-value-12345", "local-value-12345"} <= set(values)
        assert values == sorted(values, key=len, reverse=True)


class TestDiffLines:
    def test_added_and_removed_with_line_numbers(self):
        diff = (
            "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
            "@@ -2 +2,2 @@\n-old\n+new\n+-- not a header\n"
            "diff --git a/g b/g\n--- a/g\n+++ /dev/null\n@@ -1 +0,0 @@\n--- removed dashes\n"
        )
        added, removed = rg.diff_lines(diff)
        assert added == [("f", 2, "new"), ("f", 3, "-- not a header")]
        assert removed == [("f", 2, "old"), ("g", 1, "-- removed dashes")]


class TestScan:
    def test_cumulative_per_commit_and_messages(self, repo, tmp_path):
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": "contact someone@example.org\n"}, "add contact")
        commit(repo, {"a.txt": "contact removed\n"}, "remove contact\n\nping other@example.org")
        out = tmp_path / "out.json"
        status = rg.main(["scan", "--repo", str(repo), "--config", str(write_config(tmp_path)),
                          "--base", base, "--candidate", "HEAD", "--out", str(out)])
        assert status == rg.EXIT_OK
        data = json.loads(out.read_text())
        assert data["cumulative"]["hits"] == []
        first, second = data["commits"]
        assert cats(first["added"]) == {"email"}
        assert cats(second["removed"]) == {"email"}
        message_hits = [h for h in data["messages"]["hits"] if h["file"].endswith(":message")]
        assert [h["cat"] for h in message_hits] == ["email"]
        identity = [h for h in data["messages"]["hits"] if not h["file"].endswith(":message")]
        assert len(identity) == 4
        assert "someone@example.org" not in out.read_text()

    def test_published_narrows_the_per_commit_range(self, repo, tmp_path):
        base = git(repo, "rev-parse", "HEAD")
        published = commit(repo, {"a.txt": "one\n"}, "one")
        commit(repo, {"b.txt": "two\n"}, "two")
        out = tmp_path / "out.json"
        rg.main(["scan", "--repo", str(repo), "--config", str(write_config(tmp_path)),
                 "--base", base, "--candidate", "HEAD", "--published", published, "--out", str(out)])
        data = json.loads(out.read_text())
        assert len(data["commits"]) == 1
        assert data["cumulative"]["lines"] == 2

    def test_merges_are_checked_against_merge_tree(self, repo, tmp_path):
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "side")
        commit(repo, {"side.txt": "side\n"}, "side")
        git(repo, "checkout", "-q", "main")
        commit(repo, {"main.txt": "main\n"}, "main")
        git(repo, "merge", "-q", "--no-ff", "-m", "clean merge", "side")
        clean = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "side2", base)
        commit(repo, {"s2.txt": "s2\n"}, "s2")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "--no-commit", "side2")
        (repo / "evil.txt").write_text("sneaked in someone@example.org\n")
        git(repo, "add", "evil.txt")
        git(repo, "commit", "-q", "-m", "evil merge")
        commits = rg.scan_commits(repo, base, "HEAD", scanner())
        merges = {c["sha"]: c for c in commits if c["kind"] == "merge"}
        assert merges[clean]["clean_auto"] is True and "added" not in merges[clean]
        evil = merges[git(repo, "rev-parse", "HEAD")]
        assert evil["clean_auto"] is False
        assert cats(evil["added"]) == {"email"}

    def test_root_commit_is_scanned_whole(self, repo):
        published = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "--orphan", "unrelated")
        git(repo, "rm", "-q", "-r", "--cached", ".")
        root = commit(repo, {"orphan.txt": "someone@example.org\n"}, "orphan root")
        (row,) = rg.scan_commits(repo, published, root, scanner())
        assert row["kind"] == "root"
        assert cats(row["added"]) == {"email"}

    def test_live_secret_exits_2(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("FIXTURE_TOKEN", FAKE_LIVE)
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"leak.txt": f"{FAKE_LIVE}\n"}, "leak")
        out = tmp_path / "out.json"
        status = rg.main(["scan", "--repo", str(repo), "--config", str(write_config(tmp_path)),
                          "--base", base, "--candidate", "HEAD", "--out", str(out)])
        assert status == rg.EXIT_FINDING
        assert FAKE_LIVE not in out.read_text()

    def test_scan_text(self, tmp_path, capsys):
        text = tmp_path / "comment.txt"
        text.write_text("False positive: validated before the sink.\nmail someone@example.org\n")
        out = tmp_path / "out.json"
        status = rg.main(["scan-text", str(text), "--config", str(write_config(tmp_path)),
                          "--repo", str(tmp_path), "--out", str(out)])
        assert status == rg.EXIT_OK
        assert [h["line"] for h in json.loads(out.read_text())["text"]["hits"]] == [2]
        assert "email" in capsys.readouterr().out


class TestConfig:
    def test_missing_block_is_a_hard_stop(self, tmp_path):
        with pytest.raises(rg.GuardError, match="release.leak_scan is not set"):
            rg.load_config(write_config(tmp_path, block=None))

    def test_missing_block_switch_falls_back_to_generic(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rg, "REQUIRE_LEAK_SCAN_BLOCK", False)
        config = rg.load_config(write_config(tmp_path, block=None))
        assert config == rg.LeakScanConfig(public_mirror=True)

    def test_full_block(self, tmp_path):
        sha = "a" * 40
        config = rg.load_config(write_config(tmp_path, (
            "public_mirror: true\nexceptions: leak-scan-exceptions.md\n"
            f"ancestry_denylist: ['{sha}']\n"
            "patterns:\n  - name: codename\n    regex: 'bluefin'\n"
            "guard_strings: ['secret-project']\n"
        )))
        assert config.exceptions == "leak-scan-exceptions.md"
        assert config.ancestry_denylist == (sha,)
        assert [name for name, _ in config.patterns] == ["codename"]
        assert config.guard_strings[0].search("SECRET-PROJECT")

    @pytest.mark.parametrize("block, message", [
        ("public_mirror: yes-please", "exactly true or false"),
        ("exceptions: x", "public_mirror"),
        ("public_mirror: true\nguard_string: [x]", "unknown key"),
        ("public_mirror: true\nancestry_denylist: [abc123]", "full 40-hex"),
        ("public_mirror: true\npatterns:\n  - name: email\n    regex: x", "already in use"),
        ("public_mirror: true\npatterns:\n  - name: Bad\n    regex: x", "lowercase"),
        ("public_mirror: true\npatterns:\n  - name: ok\n    regex: '('", "not a valid regex"),
        ("public_mirror: true\nguard_strings: ['[']", "not a valid regex"),
    ])
    def test_invalid_blocks_are_refused(self, tmp_path, block, message):
        with pytest.raises(rg.GuardError, match=message):
            rg.load_config(write_config(tmp_path, block))

    def test_check_config_reports_opt_out(self, tmp_path, capsys):
        status = rg.main(["check-config", "--config", str(write_config(tmp_path, "public_mirror: false"))])
        assert status == rg.EXIT_OK
        assert "opted out" in capsys.readouterr().out

    def test_check_config_fails_loudly(self, tmp_path, capsys):
        status = rg.main(["check-config", "--config", str(write_config(tmp_path, block=None))])
        assert status == rg.EXIT_ERROR
        assert "release.leak_scan is not set" in capsys.readouterr().err


class TestPrepush:
    def _config(self, **kw):
        return rg.LeakScanConfig(public_mirror=True, **kw)

    def _run(self, repo, obj, ref, published, config=None, secrets=()):
        return rg.prepush(repo, obj, ref, "1.2.3", published, config or self._config(), list(secrets))

    @pytest.mark.parametrize("ref, ok", [
        ("refs/heads/main", True),
        ("refs/tags/v1.2.3", True),
        ("refs/heads/release-candidate/v1.2.3", True),
        ("refs/tags/v1.2.4", False),
        ("refs/heads/release-candidate/v1.2.4", False),
        ("refs/heads/prep-release", False),
        ("main", False),
    ])
    def test_ref_allow_list(self, repo, ref, ok):
        results = self._run(repo, "HEAD", ref, "HEAD")
        assert results[0][0] is ok

    def test_denylisted_ancestor_fails(self, repo):
        bad = git(repo, "rev-parse", "HEAD")
        published = bad
        commit(repo, {"a.txt": "a\n"})
        results = self._run(repo, "HEAD", "refs/heads/main", published,
                            self._config(ancestry_denylist=(bad,)))
        assert any(not ok and "IS an ancestor" in line for ok, line in results)

    def test_denylisted_non_ancestor_passes(self, repo):
        start = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "other")
        bad = commit(repo, {"x.txt": "x\n"})
        git(repo, "checkout", "-q", "main")
        results = self._run(repo, "HEAD", "refs/heads/main", start, self._config(ancestry_denylist=(bad,)))
        assert all(ok for ok, _ in results)

    def test_denylisted_absent_object_passes(self, repo):
        results = self._run(repo, "HEAD", "refs/heads/main", "HEAD",
                            self._config(ancestry_denylist=("b" * 40,)))
        assert all(ok for ok, _ in results)
        assert any("absent locally" in line for _, line in results)

    def test_shallow_clone_cannot_prove_ancestry(self, repo, tmp_path):
        commit(repo, {"a.txt": "a\n"})
        shallow = tmp_path / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True)
        results = self._run(shallow, "HEAD", "refs/heads/main", "HEAD",
                            self._config(ancestry_denylist=("c" * 40,)))
        assert any(not ok and "shallow" in line for ok, line in results)

    def test_guard_strings_fail_case_insensitively(self, repo):
        published = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": "mentions Secret-Project here\n"})
        config = rg.parse_leak_scan({"public_mirror": True, "guard_strings": ["secret-project"]}, "t")
        results = self._run(repo, "HEAD", "refs/heads/main", published, config)
        assert any(not ok and line.startswith("guard_strings[0]: 1 match") for ok, line in results)

    def test_guard_strings_cover_messages_and_identities(self, repo, monkeypatch):
        published = git(repo, "rev-parse", "HEAD")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "dev@secret-project.invalid")
        commit(repo, {"a.txt": "clean\n"}, "clean change")
        config = rg.parse_leak_scan({"public_mirror": True, "guard_strings": ["secret-project"]}, "t")
        results = self._run(repo, "HEAD", "refs/heads/main", published, config)
        assert any(not ok for ok, _ in results)

    def test_live_secret_in_range_fails(self, repo):
        published = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": f"{FAKE_LIVE}\n"})
        results = self._run(repo, "HEAD", "refs/heads/main", published, secrets=[FAKE_LIVE])
        assert any(not ok and line.startswith("live secrets: 1") for ok, line in results)
        assert not any(FAKE_LIVE in line for _, line in results)

    def test_cli_pass_and_fail(self, repo, tmp_path, capsys):
        config = write_config(tmp_path)
        args = ["prepush", "--repo", str(repo), "--config", str(config), "--version", "1.2.3",
                "--published", "HEAD", "HEAD"]
        assert rg.main(args + ["refs/heads/main"]) == rg.EXIT_OK
        assert "GUARD PASS" in capsys.readouterr().out
        assert rg.main(args + ["refs/heads/feature"]) == rg.EXIT_ERROR
        assert "GUARD FAIL" in capsys.readouterr().out

    def test_unknown_object_is_an_error(self, repo, tmp_path, capsys):
        status = rg.main(["prepush", "--repo", str(repo), "--config", str(write_config(tmp_path)),
                          "--version", "1.2.3", "--published", "HEAD", "nope", "refs/heads/main"])
        assert status == rg.EXIT_ERROR
        assert re.search(r"lmer-release-guard: git rev-parse", capsys.readouterr().err)


class TestReviewHardening:
    """Each case here once passed silently: a scan that read nothing, a guard
    that missed what a merge or a tag object publishes, config that dropped
    a check."""

    @pytest.mark.parametrize("settings", [
        ["diff.noprefix", "true"],
        ["diff.srcPrefix", "x/"],
        ["diff.mnemonicPrefix", "true"],
    ])
    def test_user_diff_config_cannot_blind_the_scan(self, repo, settings):
        git(repo, "config", *settings)
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": "someone@example.org\n"})
        added, _, _ = rg.changed_lines(repo, base, "HEAD")
        assert added == [("a.txt", 1, "someone@example.org")]

    @pytest.mark.parametrize("name", ['we"ird.txt', "tab\tname.txt", "back\\slash.txt", "spa ce.txt"])
    def test_quoted_file_names_are_scanned(self, repo, name):
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {name: "connect 10.1.2.3\n"})
        added, _, _ = rg.changed_lines(repo, base, "HEAD")
        assert added == [(name, 1, "connect 10.1.2.3")]

    def test_a_partial_parse_is_an_error_not_a_clean_scan(self, repo, monkeypatch):
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": "one\ntwo\n"})
        monkeypatch.setattr(rg, "diff_lines", lambda text: ([], []))
        with pytest.raises(rg.GuardError, match="refusing to report a partial scan"):
            rg.changed_lines(repo, base, "HEAD")

    def test_guard_string_in_a_merge_resolution_fails_the_guard(self, repo):
        published = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "side")
        commit(repo, {"side.txt": "side\n"})
        git(repo, "checkout", "-q", "main")
        commit(repo, {"main.txt": "main\n"})
        git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
        (repo / "evil.txt").write_text("secret-project\n")
        git(repo, "add", "evil.txt")
        git(repo, "commit", "-q", "-m", "merge")
        config = rg.parse_leak_scan({"public_mirror": True, "guard_strings": ["secret-project"]}, "t")
        results = rg.prepush(repo, "HEAD", "refs/heads/main", "1.2.3", published, config, [])
        assert any(not ok and "guard_strings[0]" in line for ok, line in results)

    def test_annotated_tag_message_is_guarded(self, repo):
        git(repo, "tag", "-a", "v1.2.3", "-m", "Release 1.2.3 secret-project")
        config = rg.parse_leak_scan({"public_mirror": True, "guard_strings": ["secret-project"]}, "t")
        results = rg.prepush(repo, "v1.2.3", "refs/tags/v1.2.3", "1.2.3", "HEAD", config, [])
        assert any(not ok and "guard_strings[0]: 1 match" in line for ok, line in results)

    def test_scan_reports_guard_strings_as_findings(self, repo, tmp_path):
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": "mentions Secret-Project\n"})
        out = tmp_path / "out.json"
        config = write_config(tmp_path, "public_mirror: true\nguard_strings: ['secret-project']")
        status = rg.main(["scan", "--repo", str(repo), "--config", str(config),
                          "--base", base, "--candidate", "HEAD", "--out", str(out)])
        assert status == rg.EXIT_FINDING
        assert json.loads(out.read_text())["summary"][rg.GUARD_CATEGORY] == 2  # cumulative + commit

    @pytest.mark.parametrize("block", [
        "public_mirror: true\nguard_strings: {}",
        "public_mirror: true\npatterns: ''",
        "public_mirror: true\nancestry_denylist: 0",
        "public_mirror: true\nguard_strings: null",
    ])
    def test_wrong_typed_empty_values_are_refused(self, tmp_path, block):
        with pytest.raises(rg.GuardError, match="must be a list"):
            rg.load_config(write_config(tmp_path, block))

    def test_duplicate_keys_are_refused(self, tmp_path):
        path = tmp_path / "task.yaml"
        path.write_text("release:\n  leak_scan:\n    public_mirror: true\n"
                        "  leak_scan:\n    public_mirror: false\n")
        with pytest.raises(rg.GuardError, match="duplicate key 'leak_scan'"):
            rg.load_config(path)

    @pytest.mark.parametrize("cat, text", [
        ("pem-private", "HEADER=BEGIN " + "OPENSSH PRIVATE" + " KEY"),
        ("aws-keyid", "AKIA" + "Z" * 17),
    ])
    def test_old_tool_detectors_kept(self, cat, text):
        assert cat in cats(scanner().scan([("f", 1, text)]))

    def test_local_values_lose_their_quotes(self, repo):
        (repo / "LOCAL").mkdir()
        (repo / "LOCAL" / "env").write_text('export SOMETHING="quoted-value-123"\n')
        assert "quoted-value-123" in rg.live_secrets(repo)


DOC = Path(__file__).parent.parent / "docs" / "RELEASE-GUARD.md"


def scan_json(repo, tmp_path, base, config=None, extra=()):
    out = tmp_path / "out.json"
    status = rg.main(["scan", "--repo", str(repo), "--config", str(config or write_config(tmp_path)),
                      "--base", base, "--candidate", "HEAD", "--out", str(out), *extra])
    return status, json.loads(out.read_text())


class TestScanCompleteness:
    """Ranges the first version could not scan whole, or scanned with a
    false finding: a locator held as a secret, binary files, line separators
    other than newline, textconv drivers."""

    def test_signing_key_locator_is_not_a_live_secret(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_RELEASE_SIGNING_KEY", "/release-signing-key")
        published = git(repo, "rev-parse", "HEAD")
        commit(repo, {"docs/setup.md": "The key is mounted at /release-signing-key.\n"})
        assert rg.live_secrets(repo) == []
        status, _ = scan_json(repo, tmp_path, published)
        assert status == rg.EXIT_OK
        results = rg.prepush(repo, "HEAD", "refs/heads/main", "1.2.3", published,
                             rg.load_config(write_config(tmp_path)), rg.live_secrets(repo))
        assert all(ok for ok, _ in results)

    def _binary(self, repo, name, payload):
        (repo / name).write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + payload + b"\x00\xff")
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", f"add {name}")

    def test_binary_files_are_byte_scanned_not_an_error(self, repo, tmp_path):
        base = git(repo, "rev-parse", "HEAD")
        self._binary(repo, "clean.png", b"pixels")
        self._binary(repo, "leaky.png", FAKE_PAT.encode())
        status, data = scan_json(repo, tmp_path, base)
        assert status == rg.EXIT_OK
        assert data["cumulative_binary"]["files"] == ["clean.png", "leaky.png"]
        assert {(h["file"], h["cat"]) for h in data["cumulative_binary"]["hits"]} == {
            ("leaky.png", "token-prefix"), ("leaky.png", "token-shape")}
        assert data["cumulative"]["hits"] == []
        assert [c["binary"]["files"] for c in data["commits"]] == [["clean.png"], ["leaky.png"]]
        assert FAKE_PAT not in json.dumps(data)

    def test_held_value_in_a_binary_file_is_a_finding(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("FIXTURE_TOKEN", FAKE_LIVE)
        base = git(repo, "rev-parse", "HEAD")
        self._binary(repo, "blob.bin", FAKE_LIVE.encode())
        status, data = scan_json(repo, tmp_path, base)
        assert status == rg.EXIT_FINDING
        assert data["summary"][rg.LIVE_CATEGORY] == 2  # cumulative + commit

    def test_project_patterns_are_searched_in_a_binary_file(self, repo, tmp_path):
        config = write_config(tmp_path, "public_mirror: true\n"
                              "patterns:\n  - name: build-host\n    regex: 'build\\.example\\.invalid'\n"
                              "guard_strings: ['bluefin']")
        base = git(repo, "rev-parse", "HEAD")
        self._binary(repo, "shot.png", b"tEXt\x00Comment\x00build.example.invalid Bluefin")
        status, data = scan_json(repo, tmp_path, base, config)
        assert status == rg.EXIT_FINDING
        assert [(h["file"], h["cat"]) for h in data["cumulative_binary"]["hits"]] == [
            ("shot.png", "build-host"), ("shot.png", rg.GUARD_CATEGORY)]
        assert "build.example.invalid" not in json.dumps(data)

    def test_text_marked_binary_keeps_its_line_scan(self, repo, tmp_path):
        commit(repo, {".gitattributes": "*.dat binary\n"})
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"notes.dat": "mail someone@example.org\n"})
        status, data = scan_json(repo, tmp_path, base)
        assert status == rg.EXIT_OK
        assert cats(data["cumulative"]) == {"email"}
        assert data["cumulative_binary"]["files"] == []

    def test_binary_deletion_scans_nothing(self, repo, tmp_path):
        self._binary(repo, "old.png", b"pixels")
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"old.png": None})
        status, data = scan_json(repo, tmp_path, base)
        assert status == rg.EXIT_OK
        assert data["cumulative_binary"] == {"files": [], "hits": []}

    @pytest.mark.parametrize("sep", ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "])
    def test_only_newline_ends_a_line(self, repo, tmp_path, sep):
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": f"x = 1{sep}{FAKE_PAT}\n"}, f"subject\n\nbody{sep}{FAKE_PAT}")
        status, data = scan_json(repo, tmp_path, base)
        assert status == rg.EXIT_OK
        assert "token-prefix" in cats(data["cumulative"])
        assert "token-prefix" in cats(data["commits"][0]["added"])
        assert "token-prefix" in cats(data["messages"])
        text = tmp_path / "comment.txt"
        text.write_text(f"fine{sep}{FAKE_PAT}\n", newline="")
        out = tmp_path / "text.json"
        rg.main(["scan-text", str(text), "--config", str(write_config(tmp_path)), "--repo", str(repo),
                 "--out", str(out)])
        assert "token-prefix" in cats(json.loads(out.read_text())["text"])

    def test_textconv_driver_cannot_hide_content(self, repo, tmp_path):
        commit(repo, {".gitattributes": "*.txt diff=hide\n"})
        git(repo, "config", "diff.hide.textconv", "/bin/echo SAFE")
        published = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": "mentions secret-project\n"})
        config = write_config(tmp_path, "public_mirror: true\nguard_strings: ['secret-project']")
        status, data = scan_json(repo, tmp_path, published, config)
        assert status == rg.EXIT_FINDING
        assert data["summary"][rg.GUARD_CATEGORY] == 2
        results = rg.prepush(repo, "HEAD", "refs/heads/main", "1.2.3", published, rg.load_config(config), [])
        assert any(not ok and "guard_strings[0]: 1 match" in line for ok, line in results)

    def test_host_inventory_omits_what_a_hit_already_masks(self):
        config = rg.parse_leak_scan({"public_mirror": True, "patterns": [
            {"name": "build-host", "regex": r"build\.example\.net"}]}, "t")
        result = rg.Scanner(config, ["vault.example.org"]).scan([
            ("f", 1, "see build.example.net, vault.example.org and docs.example.com")])
        assert set(result["hosts"]) == {"docs.example.com"}

    def test_record_separator_in_a_message_is_scanned(self, repo, tmp_path):
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, {"a.txt": "a\n"}, "subject\x1e with separator\n\nmail someone@example.org")
        status, data = scan_json(repo, tmp_path, base)
        assert status == rg.EXIT_OK
        assert "email" in cats(data["messages"])

    def test_the_documented_example_block_loads(self, tmp_path):
        text = DOC.read_text(encoding="utf-8")
        block = re.search(r"```yaml\n(release:\n.*?)```", text, re.S).group(1)
        block = block.replace("<full 40-hex SHA>", "a" * 40).replace("<owner>/<repo>", "example/repo")
        path = tmp_path / "task.yaml"
        path.write_text(block)
        config = rg.load_config(path)
        assert config.public_mirror and config.patterns and config.guard_strings
