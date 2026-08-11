"""Guards for the work-repo test-isolation contract (issue #93).

The render-pipeline tests (test_start_hook.py, test_self_dev.py) reach
run_state_session_start(), which shells out to the real `work` CLI; their
autouse _clean_lmer_env fixtures are what keep test runs from seeding,
claiming, or mutating runs in the operational work repo. This module
unit-tests the conftest leak-guard helper and — in the spirit of the
repo's other source-level guard tests — asserts the isolation fixtures
cannot be silently dropped.
"""
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import _work_repo_status_lines

pytest_plugins = "pytester"

TESTS_DIR = Path(__file__).parent


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    )


class TestWorkRepoLeakGuardHelper:
    """_work_repo_status_lines() — the conftest leak guard's snapshot."""

    def test_none_when_path_missing(self, tmp_path):
        assert _work_repo_status_lines(tmp_path / "missing") is None

    def test_none_when_not_a_git_repo(self, tmp_path):
        assert _work_repo_status_lines(tmp_path) is None

    def test_none_when_nested_inside_enclosing_repo(self, tmp_path):
        # `git -C` discovers enclosing repos upward; a non-repo directory
        # nested inside one must not snapshot the ancestor's status.
        _git(tmp_path, "init", "-q")
        nested = tmp_path / "not-a-repo"
        nested.mkdir()
        assert _work_repo_status_lines(nested) is None

    def test_clean_repo_snapshots_empty(self, tmp_path):
        _git(tmp_path, "init", "-q")
        (tmp_path / "tracked.txt").write_text("v1\n")
        _git(tmp_path, "add", "tracked.txt")
        _git(tmp_path, "commit", "-q", "-m", "seed")
        assert _work_repo_status_lines(tmp_path) == frozenset()

    def test_snapshots_untracked_and_modified(self, tmp_path):
        _git(tmp_path, "init", "-q")
        (tmp_path / "tracked.txt").write_text("v1\n")
        _git(tmp_path, "add", "tracked.txt")
        _git(tmp_path, "commit", "-q", "-m", "seed")

        before = _work_repo_status_lines(tmp_path)
        # The two leak signatures from issue #93: a new untracked run dir
        # and a mutation of already-tracked run state.
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "state.yaml").write_text("leak\n")
        (tmp_path / "tracked.txt").write_text("v2\n")
        after = _work_repo_status_lines(tmp_path)

        leaked = after - before
        assert any(line.startswith("??") for line in leaked)
        assert any("tracked.txt" in line and not line.startswith("??")
                   for line in leaked)

    def test_non_ascii_paths_snapshot_unquoted_and_parse(self, tmp_path):
        """`quotePath=false` keeps non-ASCII paths literal rather than
        octal-escaped. The decoder handles the escaped form too, so this is
        not the only thing standing between an accented session artifact and
        a false whole-run failure — it keeps the common case exact, and the
        raw bytes it produces are what `errors="surrogateescape"` above then
        has to survive."""
        from tests.conftest import porcelain_entry_path

        _git(tmp_path, "init", "-q")
        (tmp_path / "résumé.md").write_text("x\n")
        lines = _work_repo_status_lines(tmp_path)
        entry = next(line for line in lines if "sum" in line)
        assert entry == "?? résumé.md"
        assert porcelain_entry_path(entry) == "résumé.md"

    def test_a_non_utf8_filename_does_not_disarm_the_snapshot(self, tmp_path):
        """With quotePath=false git emits raw bytes, so one undecodable name
        used to raise UnicodeDecodeError → None → "no work repo to guard",
        silently skipping the entire run including the blame path. Every
        other degradation here fails loud (MR !200 review round 2)."""
        _git(tmp_path, "init", "-q")
        (tmp_path / "ordinary.md").write_text("x\n")
        try:
            with open(bytes(tmp_path) + b"/bad\xffname.md", "wb") as fh:
                fh.write(b"x")
        except OSError:
            pytest.skip("filesystem rejects non-UTF-8 filenames")
        lines = _work_repo_status_lines(tmp_path)
        assert lines is not None, "one odd filename must not disarm the guard"
        assert any("ordinary.md" in line for line in lines)

    def test_sees_new_file_inside_already_untracked_dir(self, tmp_path):
        # Without --untracked-files=all, porcelain collapses untracked
        # dirs to one "?? runs/" line and a new file inside one would not
        # change the snapshot — the second half of #93's leak signature.
        _git(tmp_path, "init", "-q")
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "existing.yaml").write_text("x\n")

        before = _work_repo_status_lines(tmp_path)
        (tmp_path / "runs" / "new-leak.yaml").write_text("y\n")
        after = _work_repo_status_lines(tmp_path)

        assert any("new-leak.yaml" in line for line in after - before)


class TestIsolationFixtureSourceGuard:
    """The env-isolation fixtures must not be silently removed."""

    STRIP_FIXTURE_RE = re.compile(
        r"@pytest\.fixture\(autouse=True\)\s*\n"
        r"def _clean_lmer_env\(monkeypatch\):"
    )

    @pytest.mark.parametrize(
        "module_file", ["test_start_hook.py", "test_self_dev.py"]
    )
    def test_module_keeps_strip_all_fixture(self, module_file):
        source = (TESTS_DIR / module_file).read_text()
        assert self.STRIP_FIXTURE_RE.search(source), (
            f"tests/{module_file} must keep its autouse _clean_lmer_env "
            "fixture: its tests reach `work session-start` and without the "
            "fixture they seed/claim runs in the operational work repo "
            "(issue #93)."
        )
        assert "strip_lmer_env(monkeypatch)" in source, (
            f"tests/{module_file}'s _clean_lmer_env fixture must delegate "
            "to the shared conftest strip_lmer_env helper (issue #93)."
        )

    def test_conftest_keeps_strip_helper(self):
        source = (TESTS_DIR / "conftest.py").read_text()
        assert re.search(r"\ndef strip_lmer_env\(monkeypatch\):", source), (
            "tests/conftest.py must keep the shared strip_lmer_env helper "
            "the per-module _clean_lmer_env fixtures delegate to (issue #93)."
        )
        assert 'key.startswith("LMER_")' in source, (
            "tests/conftest.py's strip_lmer_env must strip all LMER_* env "
            "vars (issue #93)."
        )

    def test_conftest_explains_an_outdated_tree_instead_of_a_bare_importerror(self):
        """On a pre-#233 image a bare `pytest` resolves lmer from the
        operational install, which lacks write_journal — the failure must say
        the image is outdated (and how to run the suite anyway), not surface
        a bare ImportError (or a TypeError from fixture setup for the
        gate_lock signature half of the same skew)."""
        source = (TESTS_DIR / "conftest.py").read_text()
        assert re.search(
            r"except ImportError.*\n\s*raise ImportError\(_OUTDATED_TREE_MSG\)",
            source,
        ), "conftest.py must wrap the write_journal import in the outdated-tree guard"
        assert "inspect.signature(gate_lock.hold_gate_lock).parameters" in source, (
            "conftest.py must also catch a pre-#233 gate_lock (signature skew)"
        )
        assert "gate-check" in source and "#198" in source, (
            "the outdated-tree message must say how to run the suite and why"
        )

    def test_conftest_keeps_leak_guard(self):
        source = (TESTS_DIR / "conftest.py").read_text()
        assert re.search(
            r'@pytest\.fixture\(autouse=True, scope="session"\)\s*\n'
            r"def _work_repo_leak_guard\(_isolate_gate_lock_dir\):",
            source,
        ), (
            "tests/conftest.py must keep the session-scoped "
            "_work_repo_leak_guard fixture that fails the suite when tests "
            "write into the real work repo (issue #93)."
        )


class TestLeakGuardEndToEnd:
    """The guard fixture's wiring, driven through a real pytest subprocess.

    Each case copies the real tests/conftest.py (self-contained) into a
    pytester dir, points LMER_WORK_REPO_PATH at a scratch git repo, runs a
    probe test in a pytest subprocess, and asserts on the whole run's
    outcome — covering env read → before/after snapshot diff → teardown
    verdict, not just the snapshot helper.
    """

    def _fake_work_repo(self, tmp_path):
        fake = tmp_path / "fake-work"
        fake.mkdir()
        _git(fake, "init", "-q")
        return fake

    def _run_probe(self, pytester, monkeypatch, tmp_path, probe_source):
        fake = self._fake_work_repo(tmp_path)
        pytester.makeconftest((TESTS_DIR / "conftest.py").read_text())
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(fake))
        pytester.makepyfile(test_probe=probe_source)
        return fake, pytester.runpytest_subprocess("-q")

    def test_clean_suite_passes(self, pytester, monkeypatch, tmp_path):
        _, result = self._run_probe(
            pytester, monkeypatch, tmp_path,
            "def test_probe():\n    pass\n",
        )
        assert result.ret == 0
        result.assert_outcomes(passed=1)

    def test_fails_naming_appeared_path(self, pytester, monkeypatch, tmp_path):
        _, result = self._run_probe(
            pytester, monkeypatch, tmp_path,
            "import os\n"
            "from pathlib import Path\n"
            "def test_probe():\n"
            "    Path(os.environ['LMER_WORK_REPO_PATH'], 'leaked.txt')"
            ".write_text('x')\n",
        )
        assert result.ret != 0
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(
            ["*operational work repo*", "*?? leaked.txt*"]
        )

    def test_fails_naming_vanished_path(self, pytester, monkeypatch, tmp_path):
        # The auto-deleter failure mode: an untracked file present at suite
        # start is deleted by a test — the guard must name it as vanished.
        fake = self._fake_work_repo(tmp_path)
        (fake / "doomed.txt").write_text("precious unpushed state\n")
        pytester.makeconftest((TESTS_DIR / "conftest.py").read_text())
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(fake))
        pytester.makepyfile(
            test_probe=(
                "import os\n"
                "from pathlib import Path\n"
                "def test_probe():\n"
                "    (Path(os.environ['LMER_WORK_REPO_PATH']) / "
                "'doomed.txt').unlink()\n"
            )
        )
        result = pytester.runpytest_subprocess("-q")
        assert result.ret != 0
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(["*vanished*", "*?? doomed.txt*"])

    def test_fails_when_work_repo_deleted(self, pytester, monkeypatch, tmp_path):
        _, result = self._run_probe(
            pytester, monkeypatch, tmp_path,
            "import os\n"
            "import shutil\n"
            "def test_probe():\n"
            "    shutil.rmtree(os.environ['LMER_WORK_REPO_PATH'])\n",
        )
        assert result.ret != 0
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(
            ["*snapshottable at suite start but not at teardown*"]
        )
