"""Tests for the gate's test-result cache (src/lmer_cli/gate_cache.py, #269)."""
import dataclasses
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from lmer_cli import gate_cache, precommit_cache

REPO_ROOT = Path(__file__).resolve().parent.parent

# The two invocations whose keys must never be confused: the whole tree, and
# the text-diff subset MR A runs for a prose-only change.
FULL_ARGV = ["python3", "-m", "pytest", "tests/", "-x", "--tb=short", "-q"]
SUBSET_ARGV = ["python3", "-m", "pytest", "tests/test_alpha.py", "-x",
               "--tb=short", "-q"]

INTERPRETER = ("/usr/bin/python3", "3.11.9 (main, Jan 1 2026)")

# What `environment_identity` hands `compose_key`: a digest of the venv marker
# and the distributions installed beside the interpreter.
DEPENDENCIES = "e" * 64


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    """Every test gets its own cache directory.

    Never the real one: a test writing there could hand a later gate a
    verdict no suite earned.
    """
    directory = tmp_path / "cache"
    monkeypatch.setenv(gate_cache.CACHE_DIR_ENV, str(directory))
    monkeypatch.delenv(gate_cache.DISABLE_ENV, raising=False)
    return directory


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True)


def _runner(root):
    """A :data:`gate_cache.Runner` bound to a real repository."""
    def run(command, check=False):
        proc = subprocess.run(command, cwd=root, capture_output=True,
                              text=True)
        return proc.returncode, proc.stdout, proc.stderr
    return run


@pytest.fixture
def repo(tmp_path):
    """A one-commit git repository — the fingerprint helpers shell out to git,
    so they are exercised against git's real output, not a paraphrase."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "gate@example.com")
    _git(root, "config", "user.name", "Gate Tests")
    (root / "module.py").write_text("value = 1\n")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", ".")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial")
    return root


# ---- key composition -------------------------------------------------------


class TestComposeKey:
    """What the key covers is what the cache can safely answer for."""

    def _key(self, **overrides):
        fields = {"tree": "t" * 40, "working": gate_cache.CLEAN_TREE,
                  "argv": FULL_ARGV, "interpreter": INTERPRETER,
                  "dependencies": DEPENDENCIES}
        fields.update(overrides)
        return gate_cache.compose_key(**fields)

    def test_identical_inputs_compose_the_same_key(self):
        assert self._key() == self._key()

    def test_a_different_tree_composes_a_different_key(self):
        assert self._key() != self._key(tree="a" * 40)

    def test_a_different_working_digest_composes_a_different_key(self):
        assert self._key() != self._key(working="deadbeef")

    def test_a_clean_tree_and_a_dirty_one_are_never_the_same_key(self):
        dirty = gate_cache.compose_working_digest(
            [("M ", "module.py", None)], [("module.py", "abc123")])
        assert self._key() != self._key(working=dirty)

    def test_the_subset_argv_cannot_satisfy_the_full_suite_key(self):
        """The sharpest failure mode this cache has (#269).

        MR A runs `tests.text_diff_subset` for a prose-only change. If that
        pass composed the same key as a full-suite run, the next release push
        would be answered by a run of a handful of files — a partial pass
        laundered into a full one. Both directions, because either would do
        it.
        """
        full = _fingerprint(key=self._key(argv=FULL_ARGV))
        subset = _fingerprint(key=self._key(argv=SUBSET_ARGV))
        assert full.key != subset.key
        assert gate_cache.read_pass(full) is None
        gate_cache.record_pass(subset, summary="5 passed in 0.5s",
                               argv=SUBSET_ARGV, gate="gate-check")
        assert gate_cache.read_pass(subset) is not None
        assert gate_cache.read_pass(full) is None

    def test_a_different_interpreter_version_composes_a_different_key(self):
        other = (INTERPRETER[0], "3.12.1 (main, Jan 1 2026)")
        assert self._key() != self._key(interpreter=other)

    def test_a_different_interpreter_path_composes_a_different_key(self):
        other = ("/opt/venv/bin/python3", INTERPRETER[1])
        assert self._key() != self._key(interpreter=other)

    def test_a_different_dependency_surface_composes_a_different_key(self):
        """An image rebuilt with other packages leaves the tree, the argv and
        the Python version all unchanged."""
        assert self._key() != self._key(dependencies="f" * 64)

    def test_an_unknown_dependency_surface_composes_no_key(self):
        assert self._key(dependencies=None) is None

    def test_a_format_version_bump_invalidates_every_old_key(self):
        current = self._key()
        future = self._key(version=gate_cache.CACHE_FORMAT_VERSION + 1)
        assert current != future

    @pytest.mark.parametrize("missing", ["tree", "working", "argv",
                                         "interpreter", "dependencies"])
    def test_a_missing_input_composes_no_key_at_all(self, missing):
        """Unknown must mean "run the suite", never "assume unchanged"."""
        assert self._key(**{missing: None}) is None

    def test_a_blank_argv_member_composes_no_key(self):
        assert self._key(argv=["python3", ""]) is None


class TestCacheEnvironment:
    """Which variables reach the comparison: all of them but the denylisted few."""

    def test_it_keeps_everything_that_can_reach_the_suite(self):
        given = {"PATH": "/usr/bin", "PYTEST_ADDOPTS": "-k x", "PWD": "/repo",
                 "HOME": "/home/dev", "LMER_WORK_REPO": "org/proj"}
        assert gate_cache.cache_environment(given) == given

    @pytest.mark.parametrize("name", sorted(gate_cache.VOLATILE_ENV_NAMES))
    def test_the_volatile_few_are_dropped(self, name):
        """They churn between invocations and cannot change what a test does;
        keying on them would mean the cache never hits."""
        filtered = gate_cache.cache_environment({"PATH": "/usr/bin",
                                                 name: "whatever"})
        assert filtered == {"PATH": "/usr/bin"}

    def test_the_per_shell_mise_token_is_dropped(self):
        """The variable that made the cache inert: `~/.bashrc`'s mise
        activation mints one per login shell, so three shells were three
        digests and zero hits. Nothing but mise's own hooks reads it."""
        assert "__MISE_SESSION" in gate_cache.VOLATILE_ENV_NAMES

    def test_the_directory_the_suite_runs_in_is_kept(self):
        assert "PWD" not in gate_cache.VOLATILE_ENV_NAMES


class TestEnvironmentDigest:
    """The environment half of the comparison: hashed per variable, digested
    as a whole, and never carrying a value anywhere."""

    BASE = {"PATH": "/usr/bin", "HOME": "/home/dev"}

    def _digest(self, environment):
        return gate_cache.compose_environment_digest(
            gate_cache.hash_environment(environment))

    def test_the_same_environment_digests_the_same(self):
        assert self._digest(self.BASE) == self._digest(dict(self.BASE))

    @pytest.mark.parametrize("name", sorted(gate_cache.VOLATILE_ENV_NAMES))
    def test_a_denylisted_variable_does_not_move_the_digest(self, name):
        """Part one of the fix: two login shells differ in `__MISE_SESSION`
        and nothing else, and must compare equal."""
        shell = dict(self.BASE, **{name: "session-7"})
        other = dict(self.BASE, **{name: "session-8"})
        assert self._digest(shell) == self._digest(other) == self._digest(self.BASE)

    @pytest.mark.parametrize("name,value", [("PYTEST_ADDOPTS", "-k nothing"),
                                            ("LMER_WORK_REPO", "org/proj"),
                                            ("GIT_CONFIG_COUNT", "1"),
                                            ("PYTHONPATH", "/elsewhere/src")])
    def test_any_other_variable_moves_the_digest(self, name, value):
        """The run inherits the whole environment, so the whole environment
        decides how it behaves — pytest reads PYTEST_ADDOPTS, and this suite's
        own tests branch on ambient state."""
        assert self._digest(self.BASE) != self._digest(
            dict(self.BASE, **{name: value}))

    def test_an_absent_environment_and_an_empty_one_agree(self):
        assert self._digest({}) == self._digest({})

    def test_no_value_survives_the_hashing(self):
        """These maps are written to an entry, and the environment holds
        tokens."""
        secret = "glpat-not-a-real-token"
        hashes = gate_cache.hash_environment(dict(self.BASE, TOKEN=secret))
        assert "TOKEN" in hashes
        assert secret not in json.dumps(hashes)
        assert secret not in gate_cache.compose_environment_digest(hashes)

    def test_two_variables_holding_one_value_hash_differently(self):
        """The name is hashed with the value, so an entry cannot even show
        that the same token is in two places."""
        hashes = gate_cache.hash_environment({"A": "same", "B": "same"})
        assert hashes["A"] != hashes["B"]

    def test_an_environment_that_is_not_all_strings_is_unknown(self):
        """Unknown means run the suite here too — a mapping this cannot digest
        exactly is one no verdict may rest on."""
        assert gate_cache.hash_environment({"PATH": None}) is None
        assert gate_cache.hash_environment({7: "/usr/bin"}) is None

    def test_hashing_filters_the_denylist_itself(self):
        """A caller that forgot `cache_environment` must not put a volatile
        variable back into the comparison."""
        assert "SHLVL" not in gate_cache.hash_environment(
            {"PATH": "/usr/bin", "SHLVL": "3"})


class TestDifferingNames:
    """What a miss can say: which variables, by name."""

    def _hashes(self, environment):
        return gate_cache.hash_environment(environment)

    def test_a_changed_variable_is_named(self):
        assert gate_cache.differing_names(
            self._hashes({"PATH": "/usr/bin", "TERM": "xterm"}),
            self._hashes({"PATH": "/usr/bin", "TERM": "dumb"})) == ["TERM"]

    def test_an_added_and_a_removed_variable_are_both_named(self):
        assert gate_cache.differing_names(
            self._hashes({"PATH": "/usr/bin", "GONE": "1"}),
            self._hashes({"PATH": "/usr/bin", "NEW": "1"})) == ["GONE", "NEW"]

    def test_identical_environments_differ_in_nothing(self):
        assert gate_cache.differing_names(self._hashes({"PATH": "/usr/bin"}),
                                          self._hashes({"PATH": "/usr/bin"})) == []

    def test_the_notice_names_the_variable_and_nothing_else(self):
        assert gate_cache.environment_miss_reason(["__MISE_SESSION"]) == (
            "same tree and invocation, environment differs (__MISE_SESSION)")
        assert gate_cache.describe_miss(["__MISE_SESSION"]) == (
            "Cache miss: same tree and invocation, environment differs "
            "(__MISE_SESSION)")

    def test_nothing_differing_prints_no_notice(self):
        assert gate_cache.environment_miss_reason([]) is None
        assert gate_cache.describe_miss([]) is None

    def test_a_long_list_is_summarized_rather_than_dumped(self):
        names = [f"VAR_{index:02d}" for index in range(20)]
        message = gate_cache.describe_miss(names)
        assert "VAR_00" in message and "VAR_19" not in message
        assert f"+{20 - gate_cache.MISMATCH_NAME_LIMIT} more" in message


class TestEnvironmentIdentity:
    """The dependency drift the rest of the key cannot see."""

    def _stub(self, prefix, sites):
        def run(command, check=False):
            return 0, json.dumps({"prefix": str(prefix),
                                  "sites": [str(s) for s in sites]}), ""
        return run

    def test_an_added_distribution_moves_the_identity(self, tmp_path):
        site = tmp_path / "site-packages"
        (site / "requests-2.31.0.dist-info").mkdir(parents=True)
        run = self._stub(tmp_path, [site])

        before = gate_cache.environment_identity(run, "python3")
        (site / "urllib3-2.2.1.dist-info").mkdir()

        assert before is not None
        assert gate_cache.environment_identity(run, "python3") != before

    def test_an_upgraded_distribution_moves_the_identity(self, tmp_path):
        site = tmp_path / "site-packages"
        (site / "requests-2.31.0.dist-info").mkdir(parents=True)
        run = self._stub(tmp_path, [site])

        before = gate_cache.environment_identity(run, "python3")
        (site / "requests-2.31.0.dist-info").rename(
            site / "requests-2.32.0.dist-info")

        assert gate_cache.environment_identity(run, "python3") != before

    def test_an_egg_info_counts_too(self, tmp_path):
        site = tmp_path / "site-packages"
        site.mkdir()
        run = self._stub(tmp_path, [site])
        before = gate_cache.environment_identity(run, "python3")
        (site / "lmer.egg-info").mkdir()
        assert gate_cache.environment_identity(run, "python3") != before

    def test_a_rewritten_venv_marker_moves_the_identity(self, tmp_path):
        site = tmp_path / "site-packages"
        site.mkdir()
        marker = tmp_path / "pyvenv.cfg"
        marker.write_text("version_info = 3.12.13\n")
        run = self._stub(tmp_path, [site])

        before = gate_cache.environment_identity(run, "python3")
        marker.write_text("version_info = 3.13.1\n")

        assert gate_cache.environment_identity(run, "python3") != before

    def test_a_site_directory_that_is_not_there_is_a_fact_not_an_unknown(
            self, tmp_path):
        """Interpreters name site directories they never populate (a venv's
        user site); refusing those would mean no cache at all."""
        assert gate_cache.environment_identity(
            self._stub(tmp_path, [tmp_path / "absent"]), "python3") is not None

    @pytest.mark.parametrize("stdout", ["not json", "[]", '{"sites": []}',
                                        '{"prefix": "/venv"}',
                                        '{"prefix": "/venv", "sites": [7]}'])
    def test_an_unreadable_probe_is_unknown(self, stdout):
        def run(command, check=False):
            return 0, stdout, ""
        assert gate_cache.environment_identity(run, "python3") is None

    def test_a_failing_probe_is_unknown(self):
        def run(command, check=False):
            return 1, "", "boom"
        assert gate_cache.environment_identity(run, "python3") is None

    def test_the_real_interpreter_answers(self, repo):
        """The probe has to survive contact with an actual interpreter."""
        assert gate_cache.environment_identity(_runner(repo), "python3")


# ---- the working-tree half of the key --------------------------------------


class TestParseStatus:
    def test_a_clean_tree_parses_to_no_entries(self):
        assert gate_cache.parse_status("") == []

    def test_status_and_path_are_both_kept(self):
        parsed = gate_cache.parse_status("M  module.py\0?? scratch.py\0")
        assert parsed == [("M ", "module.py", None), ("??", "scratch.py", None)]

    def test_a_rename_keeps_its_source_path(self):
        """`A -> C` and `B -> C` must not digest identically."""
        parsed = gate_cache.parse_status("R  new.py\0old.py\0")
        assert parsed == [("R ", "new.py", "old.py")]

    @pytest.mark.parametrize("text", ["nope\0", "M\0", "R  new.py\0"])
    def test_unrecognizable_output_fails_closed(self, text):
        assert gate_cache.parse_status(text) is None


class TestWorkingDigest:
    """The committed tree is not what pytest imports; this is."""

    def test_a_clean_tree_digests_to_a_stable_distinct_marker(self, repo):
        digest = gate_cache.working_tree_digest(_runner(repo))
        assert digest == gate_cache.CLEAN_TREE
        # Distinct from both "could not compute" and any real digest, so an
        # empty answer can never be mistaken for a clean tree.
        assert digest is not None
        assert digest != gate_cache.compose_working_digest(
            [("M ", "module.py", None)], [("module.py", "abc")])

    def test_an_uncommitted_edit_moves_the_digest(self, repo):
        run = _runner(repo)
        before = gate_cache.working_tree_digest(run)
        (repo / "module.py").write_text("value = 2\n")
        after = gate_cache.working_tree_digest(run)
        assert after not in (before, None)

    def test_reverting_the_edit_restores_the_digest(self, repo):
        run = _runner(repo)
        before = gate_cache.working_tree_digest(run)
        (repo / "module.py").write_text("value = 2\n")
        (repo / "module.py").write_text("value = 1\n")
        assert gate_cache.working_tree_digest(run) == before

    def test_staging_the_same_edit_is_a_different_state(self, repo):
        """Staged and unstaged are different trees to `git diff`, and the
        gate's own checks read them differently — so they are different keys."""
        run = _runner(repo)
        (repo / "module.py").write_text("value = 2\n")
        unstaged = gate_cache.working_tree_digest(run)
        _git(repo, "add", "module.py")
        assert gate_cache.working_tree_digest(run) != unstaged

    def test_an_untracked_file_moves_the_digest(self, repo):
        run = _runner(repo)
        before = gate_cache.working_tree_digest(run)
        (repo / "scratch.py").write_text("x = 1\n")
        assert gate_cache.working_tree_digest(run) not in (before, None)

    def test_a_deletion_is_recorded_without_a_blob(self, repo):
        run = _runner(repo)
        before = gate_cache.working_tree_digest(run)
        (repo / "module.py").unlink()
        assert gate_cache.working_tree_digest(run) not in (before, None)

    def test_an_untracked_directory_is_digested_file_by_file(self, repo):
        """`-uall` enumerates it, so a file added inside moves the digest
        instead of hiding behind the one summary line porcelain would print."""
        run = _runner(repo)
        (repo / "pending").mkdir()
        (repo / "pending" / "a.py").write_text("x = 1\n")
        one = gate_cache.working_tree_digest(run)
        (repo / "pending" / "b.py").write_text("y = 2\n")
        two = gate_cache.working_tree_digest(run)
        assert one is not None and two is not None
        assert one != two

    def test_a_repo_hiding_untracked_files_is_not_reported_as_clean(self, repo):
        """The forged-hit repro: `status.showUntrackedFiles=no` in the repo's
        (or the user's) config makes the porcelain silent, and an untracked
        conftest.py that deselects the whole suite then digests exactly like a
        clean tree. The flags are pinned, so the config cannot say."""
        run = _runner(repo)
        clean = gate_cache.working_tree_digest(run)
        _git(repo, "config", "status.showUntrackedFiles", "no")
        (repo / "conftest.py").write_text('collect_ignore_glob = ["*"]\n')

        digest = gate_cache.working_tree_digest(run)

        assert digest is not None
        assert digest != clean
        assert digest != gate_cache.CLEAN_TREE

    def test_a_dirty_submodule_is_unknown_however_the_repo_is_configured(
            self, repo, tmp_path):
        """`--ignore-submodules=none` for the same reason as `-uall`: the
        answer must not depend on what a config file asks git to hide."""
        inner = tmp_path / "inner"
        inner.mkdir()
        _git(inner, "init", "-q")
        _git(inner, "config", "user.email", "gate@example.com")
        _git(inner, "config", "user.name", "Gate Tests")
        (inner / "lib.py").write_text("v = 1\n")
        _git(inner, "add", ".")
        _git(inner, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "inner")
        added = _git(repo, "-c", "protocol.file.allow=always", "submodule",
                     "add", "-q", str(inner), "vendor")
        if added.returncode != 0:  # pragma: no cover - git build without file://
            pytest.skip(f"submodule add unavailable: {added.stderr.strip()}")
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "vendor")
        _git(repo, "config", "diff.ignoreSubmodules", "all")
        (repo / "vendor" / "lib.py").write_text("v = 2\n")

        assert gate_cache.working_tree_digest(_runner(repo)) is None

    def test_from_a_subdirectory_the_content_still_decides(self, repo):
        """Porcelain paths are repo-root-relative wherever the command runs.
        Resolved against a subdirectory they find nothing, every dirty path
        looks deleted, and two different edits digest identically."""
        sub = repo / "sub"
        sub.mkdir()
        (sub / "keep.py").write_text("k = 1\n")
        _git(repo, "add", "sub")
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "sub")
        run = _runner(sub)

        (repo / "module.py").write_text("value = 2\n")
        first = gate_cache.working_tree_digest(run)
        (repo / "module.py").write_text("value = 999  # totally different\n")
        second = gate_cache.working_tree_digest(run)

        assert first is not None and second is not None
        assert first != second
        # And the same tree digests the same from either directory, so a gate
        # run from a subdirectory can still reuse a pass recorded at the root.
        assert second == gate_cache.working_tree_digest(_runner(repo))

    def test_outside_a_git_repo_there_is_no_digest(self, tmp_path):
        loose = tmp_path / "loose"
        loose.mkdir()
        assert gate_cache.working_tree_digest(_runner(loose)) is None


class TestComputeFingerprint:
    def test_a_clean_repo_fingerprints_reproducibly(self, repo):
        """The property the whole feature rests on: two calls in one
        environment compose ONE key. A cache that never hits is dead."""
        run = _runner(repo)
        first = gate_cache.compute_fingerprint(run, FULL_ARGV)
        second = gate_cache.compute_fingerprint(run, FULL_ARGV)
        assert first is not None
        assert first == second
        assert first.clean is True

    def test_the_ambient_environment_still_composes_one_key(self, repo):
        """Same, with the real environment a gate hands its run — the whole of
        it is compared, so this is where over-keying would show up."""
        run = _runner(repo)
        environment = gate_cache.cache_environment(os.environ)
        first = gate_cache.compute_fingerprint(run, FULL_ARGV, environment)
        second = gate_cache.compute_fingerprint(run, FULL_ARGV, environment)
        assert first is not None
        assert first.key == second.key

    def test_two_environments_share_one_key_and_differ_in_the_digest(self, repo):
        """Where the environment lives now: out of the filename, in the entry.

        In the filename, each login shell filed its own entry under its own
        name; here both runs look under ONE name and the digest decides.
        """
        run = _runner(repo)
        first = gate_cache.compute_fingerprint(run, FULL_ARGV,
                                               {"PATH": "/usr/bin"})
        second = gate_cache.compute_fingerprint(
            run, FULL_ARGV, {"PATH": "/usr/bin", "PYTEST_ADDOPTS": "-k x"})
        assert first is not None and second is not None
        assert first.key == second.key
        assert first.environment != second.environment
        assert gate_cache.differing_names(
            first.environment_hashes,
            second.environment_hashes) == ["PYTEST_ADDOPTS"]

    def test_a_fresh_per_shell_token_leaves_the_fingerprint_identical(self, repo):
        """The whole fingerprint, not just the key: a new login shell must be
        indistinguishable from the one that recorded the pass."""
        run = _runner(repo)
        base = {"PATH": "/usr/bin", "HOME": "/home/dev"}
        first = gate_cache.compute_fingerprint(
            run, FULL_ARGV, dict(base, __MISE_SESSION="0aBcD"))
        second = gate_cache.compute_fingerprint(
            run, FULL_ARGV, dict(base, __MISE_SESSION="9zYxW"))
        assert first is not None
        assert first == second

    def test_a_commit_moves_the_key(self, repo):
        run = _runner(repo)
        before = gate_cache.compute_fingerprint(run, FULL_ARGV)
        (repo / "module.py").write_text("value = 2\n")
        _git(repo, "add", "module.py")
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "edit")
        after = gate_cache.compute_fingerprint(run, FULL_ARGV)
        assert after is not None
        assert after.key != before.key
        assert after.tree != before.tree

    def test_the_index_tree_becomes_the_committed_tree(self, repo):
        run = _runner(repo)
        (repo / "module.py").write_text("value = 2\n")
        _git(repo, "add", "module.py")

        indexed = gate_cache.indexed_tree(run)
        assert indexed is not None
        assert indexed != gate_cache.committed_tree(run)

        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "edit")
        assert gate_cache.committed_tree(run) == indexed

    def test_a_dirty_tree_says_so(self, repo):
        (repo / "module.py").write_text("value = 2\n")
        fingerprint = gate_cache.compute_fingerprint(_runner(repo), FULL_ARGV)
        assert fingerprint is not None
        assert fingerprint.clean is False

    def test_an_environment_that_cannot_be_digested_yields_no_fingerprint(
            self, repo):
        assert gate_cache.compute_fingerprint(
            _runner(repo), FULL_ARGV, {"PATH": None}) is None

    def test_outside_a_git_repo_there_is_no_fingerprint(self, tmp_path):
        loose = tmp_path / "loose"
        loose.mkdir()
        assert gate_cache.compute_fingerprint(_runner(loose), FULL_ARGV) is None

    def test_an_unknown_interpreter_yields_no_fingerprint(self, repo):
        argv = ["python-that-does-not-exist", "-m", "pytest"]
        assert gate_cache.compute_fingerprint(_runner(repo), argv) is None

    def test_an_empty_argv_yields_no_fingerprint(self, repo):
        assert gate_cache.compute_fingerprint(_runner(repo), []) is None


class TestInterpreterIdentity:
    def test_it_reports_the_probed_interpreters_own_version(self, repo):
        identity = gate_cache.interpreter_identity(_runner(repo), "python3")
        assert identity is not None
        resolved, version = identity
        assert Path(resolved).is_absolute()
        assert version.split()[0][0].isdigit()

    def test_an_interpreter_that_is_not_on_path_is_unknown(self, repo):
        assert gate_cache.interpreter_identity(
            _runner(repo), "python-that-does-not-exist") is None


# ---- entries ---------------------------------------------------------------


#: The environment a hand-built fingerprint carries unless a test says otherwise.
AMBIENT = {"PATH": "/usr/bin", "HOME": "/home/dev"}


def _fingerprint(key="k" * 64, working=gate_cache.CLEAN_TREE,
                 environment=None):
    hashes = gate_cache.hash_environment(
        AMBIENT if environment is None else environment)
    return gate_cache.Fingerprint(
        key=key, tree="t" * 40, working=working,
        environment=gate_cache.compose_environment_digest(hashes),
        environment_hashes=hashes)


def _write_entry(cache_dir, key, **fields):
    """Hand-write an entry file, the way a forger would."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    record = {"version": gate_cache.CACHE_FORMAT_VERSION,
              "outcome": "pass", "created_at": time.time()}
    record.update(fields)
    path = gate_cache.entry_path(key, cache_dir)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class TestEntries:
    def test_a_recorded_pass_is_read_back(self, _cache_dir):
        fingerprint = _fingerprint()
        path = gate_cache.record_pass(fingerprint, summary="8 passed in 1s",
                                      argv=FULL_ARGV, gate="gate-push")
        assert path is not None and path.parent == _cache_dir
        entry = gate_cache.read_pass(fingerprint)
        assert entry["summary"] == "8 passed in 1s"
        assert entry["gate"] == "gate-push"
        assert entry["argv"] == FULL_ARGV

    def test_an_absent_key_is_a_miss(self):
        assert gate_cache.read_pass(_fingerprint(key="f" * 64)) is None
        assert gate_cache.read_pass(None) is None

    def test_a_corrupt_entry_is_ignored_not_trusted(self, _cache_dir):
        _cache_dir.mkdir(parents=True)
        gate_cache.entry_path("c" * 64).write_text("{not json", encoding="utf-8")
        assert gate_cache.read_pass(_fingerprint(key="c" * 64)) is None

    def test_a_torn_write_is_ignored(self, _cache_dir):
        _cache_dir.mkdir(parents=True)
        gate_cache.entry_path("c" * 64).write_text(
            '{"version": 1, "outcome": "pa', encoding="utf-8")
        assert gate_cache.read_pass(_fingerprint(key="c" * 64)) is None

    def test_an_entry_from_another_cache_format_is_ignored(self, _cache_dir):
        """A bumped version invalidates old entries instead of misreading them."""
        _write_entry(_cache_dir, "v" * 64, tree="t" * 40,
                     working=gate_cache.CLEAN_TREE,
                     version=gate_cache.CACHE_FORMAT_VERSION - 1)
        assert gate_cache.read_pass(_fingerprint(key="v" * 64)) is None

    def test_a_non_pass_outcome_is_never_a_hit(self, _cache_dir):
        _write_entry(_cache_dir, "f" * 64, tree="t" * 40,
                     working=gate_cache.CLEAN_TREE, outcome="fail")
        assert gate_cache.read_pass(_fingerprint(key="f" * 64)) is None

    def test_an_undatable_entry_is_ignored(self, _cache_dir):
        _write_entry(_cache_dir, "d" * 64, tree="t" * 40,
                     working=gate_cache.CLEAN_TREE, created_at="yesterday")
        assert gate_cache.read_pass(_fingerprint(key="d" * 64)) is None

    def test_a_minimal_hand_written_entry_is_not_a_pass(self, _cache_dir):
        """`{version, outcome, created_at}` is what a forged entry costs to
        write. It says nothing about which tree it answers for, so it answers
        for none."""
        _write_entry(_cache_dir, "h" * 64)
        assert gate_cache.read_pass(_fingerprint(key="h" * 64)) is None

    @pytest.mark.parametrize("field", ["tree", "working"])
    def test_an_entry_that_disagrees_with_the_fingerprint_is_a_miss(
            self, _cache_dir, field):
        """Otherwise the only thing authorizing the skip is a filename — which
        also covers a hash collision and a reverted format version."""
        fingerprint = _fingerprint()
        gate_cache.record_pass(fingerprint, summary="8 passed",
                               argv=FULL_ARGV, gate="gate-check")
        assert gate_cache.read_pass(fingerprint) is not None

        _write_entry(_cache_dir, fingerprint.key,
                     **{"tree": fingerprint.tree,
                        "working": fingerprint.working,
                        "environment": fingerprint.environment,
                        field: "something-else"})

        assert gate_cache.read_pass(fingerprint) is None

    def test_an_expired_entry_is_a_miss_even_before_it_is_pruned(self):
        fingerprint = _fingerprint()
        stale = time.time() - gate_cache.MAX_ENTRY_AGE_SECONDS - 60
        gate_cache.record_pass(fingerprint, summary="1 passed",
                               argv=FULL_ARGV, gate="gate-check", now=stale)
        assert gate_cache.read_pass(fingerprint) is None

    def test_a_future_dated_entry_is_a_miss(self):
        """A clock step must not mint an entry that never expires."""
        fingerprint = _fingerprint()
        gate_cache.record_pass(fingerprint, summary="1 passed",
                               argv=FULL_ARGV, gate="gate-check",
                               now=time.time() + 3600)
        assert gate_cache.read_pass(fingerprint) is None

    def test_an_unreadable_cache_directory_is_a_miss(self, monkeypatch,
                                                     tmp_path):
        monkeypatch.setenv(gate_cache.CACHE_DIR_ENV,
                           str(tmp_path / "missing" / "deeper"))
        assert gate_cache.read_pass(_fingerprint(key="a" * 64)) is None


class TestEnvironmentEntryCheck:
    """The environment decides out of the ENTRY, not out of the filename.

    A difference is still a miss — that safety property is what the previous
    round bought — but a miss that can be named, so the next variable that
    turns out to be volatile announces itself instead of quietly costing ten
    minutes a run.
    """

    LOGIN = {"PATH": "/usr/bin", "HOME": "/home/dev"}

    def _record(self, environment):
        fingerprint = _fingerprint(environment=environment)
        gate_cache.record_pass(fingerprint, summary="8 passed",
                               argv=FULL_ARGV, gate="gate-check")
        return fingerprint

    def test_a_second_login_shell_reads_the_recorded_pass(self, _cache_dir):
        """The defect this round fixes, at unit scale: `~/.bashrc` mints a new
        `__MISE_SESSION` per login shell, and the gate ran from a new shell
        every time. Same tree, same invocation, another session token — hit."""
        self._record(dict(self.LOGIN, __MISE_SESSION="0aBcD"))
        later = _fingerprint(environment=dict(self.LOGIN,
                                              __MISE_SESSION="9zYxW"))

        assert gate_cache.read_pass(later) is not None
        assert gate_cache.environment_mismatch(later) == []

    def test_any_other_variable_is_a_miss_that_names_itself(self, _cache_dir):
        self._record(self.LOGIN)
        other = _fingerprint(
            environment=dict(self.LOGIN, PYTEST_ADDOPTS="-k nothing"))

        assert gate_cache.read_pass(other) is None
        assert gate_cache.environment_mismatch(other) == ["PYTEST_ADDOPTS"]

    def test_the_miss_names_the_variable_and_never_its_value(self, _cache_dir):
        """Not in the notice, and not in the entry that produced it."""
        secret = "glpat-not-a-real-token"
        recorded = self._record(dict(self.LOGIN, GITLAB_TOKEN="previous"))
        other = _fingerprint(environment=dict(self.LOGIN,
                                              GITLAB_TOKEN=secret))

        names = gate_cache.environment_mismatch(other)
        message = gate_cache.describe_miss(names)

        assert names == ["GITLAB_TOKEN"]
        assert message.endswith("environment differs (GITLAB_TOKEN)")
        assert secret not in message
        assert "previous" not in message
        entry = gate_cache.entry_path(recorded.key, _cache_dir).read_text(
            encoding="utf-8")
        assert secret not in entry and "previous" not in entry

    def test_one_tree_files_one_entry_however_many_shells_run_it(self,
                                                                  _cache_dir):
        """The other half of moving the environment out of the filename: a
        directory that grew a file per shell now grows one per tree."""
        self._record(self.LOGIN)
        self._record(dict(self.LOGIN, PYTEST_ADDOPTS="-k nothing"))

        assert len(list(_cache_dir.glob(f"*{gate_cache.ENTRY_SUFFIX}"))) == 1

    def test_an_entry_that_states_no_environment_is_a_miss(self, _cache_dir):
        """A hand-written entry answers for no environment, so it answers for
        none of them — the `tree`/`working` reasoning, one field further."""
        fingerprint = _fingerprint()
        _write_entry(_cache_dir, fingerprint.key, tree=fingerprint.tree,
                     working=fingerprint.working)

        assert gate_cache.read_pass(fingerprint) is None
        assert gate_cache.environment_mismatch(fingerprint) == sorted(
            fingerprint.environment_hashes)

    def test_a_mismatch_is_a_miss_and_never_an_error(self, _cache_dir):
        """Unreadable per-variable hashes cost the naming, not the gate."""
        fingerprint = _fingerprint()
        _write_entry(_cache_dir, fingerprint.key, tree=fingerprint.tree,
                     working=fingerprint.working, environment="something-else",
                     environment_hashes="not a map")

        assert gate_cache.read_pass(fingerprint) is None
        assert gate_cache.environment_mismatch(fingerprint) == sorted(
            fingerprint.environment_hashes)

    def test_a_miss_on_the_tree_is_not_reported_as_an_environment_difference(
            self, _cache_dir):
        """Pointing at the environment when the TREE moved would send the
        reader after the wrong thing; that miss explains itself already."""
        recorded = self._record(self.LOGIN)
        moved = dataclasses.replace(recorded, tree="z" * 40)

        assert gate_cache.read_pass(moved) is None
        assert gate_cache.environment_mismatch(moved) == []

    def test_nothing_recorded_names_nothing(self):
        assert gate_cache.environment_mismatch(_fingerprint()) == []
        assert gate_cache.environment_mismatch(None) == []


class TestCacheDirectorySafety:
    """An entry authorizes SKIPPING the suite, and the default directory lives
    in a world-writable /tmp: whoever can write one mints a passing gate."""

    def test_the_directory_and_its_entries_are_owner_only(self, _cache_dir):
        path = gate_cache.record_pass(_fingerprint(), summary="8 passed",
                                      argv=FULL_ARGV, gate="gate-check")
        assert stat.S_IMODE(_cache_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_directory_found_with_looser_bits_is_tightened(self, _cache_dir):
        _cache_dir.mkdir(parents=True)
        _cache_dir.chmod(0o755)

        gate_cache.record_pass(_fingerprint(), summary="8 passed",
                               argv=FULL_ARGV, gate="gate-check")

        assert stat.S_IMODE(_cache_dir.stat().st_mode) == 0o700

    def test_a_tighter_directory_is_never_widened(self, _cache_dir):
        _cache_dir.mkdir(parents=True, mode=0o500)
        assert gate_cache.safe_cache_dir() is not None
        assert stat.S_IMODE(_cache_dir.stat().st_mode) == 0o500

    def test_a_directory_owned_by_another_uid_is_refused(self, _cache_dir,
                                                          monkeypatch):
        """Refused, not adopted — and a refusal is a miss, never an error."""
        _cache_dir.mkdir(parents=True)
        gate_cache.record_pass(_fingerprint(), summary="8 passed",
                               argv=FULL_ARGV, gate="gate-check")
        # The uid is what moves, not the directory: a test cannot chown one.
        other = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: other)

        assert gate_cache.safe_cache_dir() is None
        assert gate_cache.read_pass(_fingerprint()) is None
        assert gate_cache.record_pass(_fingerprint(key="z" * 64),
                                      summary="8 passed", argv=FULL_ARGV,
                                      gate="gate-check") is None
        assert gate_cache.prune_entries() == 0

    def test_a_symlinked_directory_is_refused_not_followed(self, tmp_path,
                                                            monkeypatch):
        real = tmp_path / "elsewhere"
        real.mkdir()
        link = tmp_path / "cache-link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setenv(gate_cache.CACHE_DIR_ENV, str(link))

        assert gate_cache.safe_cache_dir(create=True) is None
        assert gate_cache.record_pass(_fingerprint(), summary="8 passed",
                                      argv=FULL_ARGV, gate="gate-check") is None
        assert list(real.iterdir()) == []

    def test_a_symlinked_entry_is_never_followed(self, _cache_dir, tmp_path):
        """The directory mode is the real defence; these are the second one.

        The write lands on a temp and is renamed over the name, and a rename
        replaces the symlink rather than following it — so a planted link is
        destroyed, never written through. The read refuses one outright.
        """
        _cache_dir.mkdir(parents=True, mode=0o700)
        target = tmp_path / "outside.json"
        fingerprint = _fingerprint()
        planted = gate_cache.entry_path(fingerprint.key, _cache_dir)
        planted.symlink_to(target)
        assert gate_cache.read_pass(fingerprint) is None

        gate_cache.record_pass(fingerprint, summary="8 passed",
                               argv=FULL_ARGV, gate="gate-check")

        assert not target.exists()
        assert not planted.is_symlink()
        assert gate_cache.read_pass(fingerprint) is not None

    def test_the_write_leaves_no_temp_behind(self, _cache_dir):
        """`os.listdir`, not a glob: the temp is dotted, and pathlib's glob
        matches dotted names, so a glob here would not see what it missed."""
        gate_cache.record_pass(_fingerprint(), summary="8 passed",
                               argv=FULL_ARGV, gate="gate-check")
        assert os.listdir(_cache_dir) == [f"{'k' * 64}{gate_cache.ENTRY_SUFFIX}"]

    def test_a_leftover_temp_expires_like_anything_else(self, _cache_dir):
        """A crashed write must not leave a file that lives forever — and it
        can never be read as an entry, since a reader opens one exact name."""
        _cache_dir.mkdir(parents=True)
        temp = _cache_dir / f".{'k' * 64}.4242{gate_cache.ENTRY_SUFFIX}"
        temp.write_text("half a rec", encoding="utf-8")
        ancient = time.time() - gate_cache.MAX_ENTRY_AGE_SECONDS * 2
        os.utime(temp, (ancient, ancient))

        assert gate_cache.read_pass(_fingerprint()) is None
        assert gate_cache.prune_entries() == 1
        assert not temp.exists()


class TestKillSwitch:
    def test_it_suppresses_both_the_read_and_the_write(self, monkeypatch,
                                                       _cache_dir):
        fingerprint = _fingerprint()
        gate_cache.record_pass(fingerprint, summary="8 passed", argv=FULL_ARGV,
                               gate="gate-check")
        monkeypatch.setenv(gate_cache.DISABLE_ENV, "1")

        assert gate_cache.read_pass(fingerprint) is None
        other = _fingerprint(key="z" * 64)
        assert gate_cache.record_pass(other, summary="8 passed",
                                      argv=FULL_ARGV, gate="gate-check") is None
        assert not gate_cache.entry_path(other.key).exists()

    def test_a_falsy_value_leaves_the_cache_on(self, monkeypatch):
        monkeypatch.setenv(gate_cache.DISABLE_ENV, "0")
        assert gate_cache.cache_enabled() is True


class TestPrune:
    def test_it_removes_entries_past_the_horizon_and_keeps_the_rest(self):
        now = time.time()
        fresh = _fingerprint(key="a" * 64)
        stale = _fingerprint(key="b" * 64)
        gate_cache.record_pass(fresh, summary=None, argv=FULL_ARGV,
                               gate="gate-check", now=now)
        gate_cache.record_pass(
            stale, summary=None, argv=FULL_ARGV, gate="gate-check",
            now=now - gate_cache.MAX_ENTRY_AGE_SECONDS - 60)

        assert gate_cache.prune_entries(now) == 1
        assert gate_cache.entry_path(fresh.key).exists()
        assert not gate_cache.entry_path(stale.key).exists()

    def test_writing_prunes_opportunistically(self, _cache_dir):
        """Nobody has to remember to sweep: the writers are the only processes
        guaranteed to visit this directory."""
        now = time.time()
        stale = _fingerprint(key="b" * 64)
        gate_cache.record_pass(
            stale, summary=None, argv=FULL_ARGV, gate="gate-check",
            now=now - gate_cache.MAX_ENTRY_AGE_SECONDS - 60)

        gate_cache.record_pass(_fingerprint(key="a" * 64), summary=None,
                               argv=FULL_ARGV, gate="gate-check", now=now)

        assert not gate_cache.entry_path(stale.key).exists()

    def test_junk_expires_on_its_mtime_rather_than_living_forever(
            self, _cache_dir):
        _cache_dir.mkdir(parents=True)
        junk = gate_cache.entry_path("j" * 64)
        junk.write_text("not an entry", encoding="utf-8")
        ancient = time.time() - gate_cache.MAX_ENTRY_AGE_SECONDS * 2
        os.utime(junk, (ancient, ancient))

        assert gate_cache.prune_entries() == 1
        assert not junk.exists()

    def test_a_missing_directory_prunes_nothing_and_raises_nothing(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv(gate_cache.CACHE_DIR_ENV, str(tmp_path / "absent"))
        assert gate_cache.prune_entries() == 0


# ---- the hit notice --------------------------------------------------------


class TestDescribeHit:
    """A skipped suite that cannot be read is indistinguishable from one
    nobody ran."""

    def _entry(self, **overrides):
        entry = {"outcome": "pass", "created_at": 1000.0, "gate": "gate-push",
                 "summary": "8727 passed, 40 skipped in 613.08s",
                 "argv": FULL_ARGV, "tree": "t" * 40}
        entry.update(overrides)
        return entry

    def test_it_names_when_what_and_who(self):
        lines = gate_cache.describe_hit(self._entry(), _fingerprint(),
                                        now=1000.0 + 4 * 60)
        assert lines[0].startswith("Proven green 4m00s ago by gate-push")
        assert "8727 passed, 40 skipped in 613.08s" in lines[0]
        assert "working tree clean" in lines[1]
        assert "same test invocation, interpreter and environment" in lines[1]
        assert lines[2] == "LMER_GATE_NO_CACHE=1 forces a re-run."

    def test_a_dirty_tree_is_never_reported_as_clean(self):
        lines = gate_cache.describe_hit(
            self._entry(), _fingerprint(working="deadbeefcafe0123"),
            now=1000.0)
        assert "working tree dirty" in lines[1]

    def test_a_summaryless_entry_still_describes_the_hit(self):
        lines = gate_cache.describe_hit(self._entry(summary=None),
                                        _fingerprint(), now=1000.0 + 90)
        assert lines[0] == "Proven green 1m30s ago by gate-push"


# ---- the container boundary ------------------------------------------------


class TestContainerEnvPassthrough:
    """Cache vars must reach INSIDE the container: the gates run there, so a
    host-side value that stopped at the boundary would be inert (the
    env-vars.md rule-4 guard, as for the gate-in-flight pair)."""

    NAMES = [
        gate_cache.DISABLE_ENV,
        gate_cache.CACHE_DIR_ENV,
        precommit_cache.CACHE_DIR_ENV,
    ]

    @pytest.mark.parametrize("name", NAMES)
    def test_cli_env_dict_declares_the_var(self, name):
        source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text(
            encoding="utf-8")
        assert f'"{name}": os.environ.get("{name}")' in source

    @pytest.mark.parametrize("name", NAMES)
    def test_documented_in_lmer_cli_docs(self, name):
        source = (REPO_ROOT / "docs" / "LMER-CLI.md").read_text(
            encoding="utf-8")
        assert f"`{name}`" in source

    def test_names_carry_the_lmer_prefix(self):
        for name in self.NAMES:
            assert name.startswith("LMER_")
