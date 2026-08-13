"""Tests for Ctl/container/container-limits.sh.

The script is sourced from the session shell's ~/.bashrc and exports
``CONTAINER_LIMITS`` derived from the container's cgroup. The real script is
executed here (not a re-implementation) against fixture cgroup trees under
``tmp_path``, which it accepts as an optional positional argument.
"""
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "Ctl" / "container" / "container-limits.sh"


def _source(root, shell_opts=""):
    """Source the script against ``root`` and return (returncode, CONTAINER_LIMITS).

    The ``.`` is a whole command of the list, not the left side of an ``&&``:
    a command on the left of ``&&`` runs with errexit suppressed, so under
    ``set -e`` that shape would swallow the very abort these tests look for.
    ~/.bashrc sources the script as the right side of one, where errexit is
    live.

    That shape costs the status, though: the last command of the list is the
    ``printf``, which succeeds whatever the sourcing returned, so a non-zero
    status from the script would reach the caller as 0 unless errexit had
    already aborted the shell. ``rc=$?`` right after the ``.`` keeps it, and
    the closing ``exit $rc`` makes it the subprocess's status — without
    putting the ``.`` back where errexit is suppressed.
    """
    script = (
        f'{shell_opts}. "$1" "$2"; rc=$?; printf %s "$CONTAINER_LIMITS"; exit $rc'
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(SCRIPT), str(root)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stderr == "", result.stderr
    return result.returncode, result.stdout


def _write_tree(root, files):
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


@pytest.fixture
def cgroup_v1(tmp_path):
    """A cgroup v1 tree: 1 CPU, 2 GiB, 32000 processes."""
    return _write_tree(tmp_path / "v1", {
        "cpu/cpu.cfs_quota_us": "100000\n",
        "cpu/cpu.cfs_period_us": "100000\n",
        "memory/memory.limit_in_bytes": "2147483648\n",
        "pids/pids.max": "32000\n",
    })


@pytest.fixture
def cgroup_v2(tmp_path):
    """A cgroup v2 tree: 2 CPUs, 4 GiB, no pids limit."""
    return _write_tree(tmp_path / "v2", {
        "cpu.max": "200000 100000\n",
        "memory.max": "4294967296\n",
        "pids.max": "max\n",
    })


class TestCgroupV1:
    def test_full_v1_tree(self, cgroup_v1):
        code, limits = _source(cgroup_v1)
        assert code == 0
        assert limits == "CPU:1core Memory:2GiB Processes:32000"

    def test_negative_quota_is_unlimited_cpu(self, cgroup_v1):
        (cgroup_v1 / "cpu" / "cpu.cfs_quota_us").write_text("-1\n")
        assert _source(cgroup_v1)[1].startswith("CPU:unlimited ")

    def test_kernel_sentinel_is_unlimited_memory(self, cgroup_v1):
        # The page-aligned LONG_MAX cgroup v1 reports for an unset limit.
        (cgroup_v1 / "memory" / "memory.limit_in_bytes").write_text(
            "9223372036854771712\n"
        )
        assert "Memory:unlimited" in _source(cgroup_v1)[1]

    def test_sub_gib_memory_is_reported_in_mib(self, cgroup_v1):
        (cgroup_v1 / "memory" / "memory.limit_in_bytes").write_text("536870912\n")
        assert "Memory:512MiB" in _source(cgroup_v1)[1]

    def test_fractional_gib_memory_keeps_one_decimal(self, cgroup_v1):
        (cgroup_v1 / "memory" / "memory.limit_in_bytes").write_text("1610612736\n")
        assert "Memory:1.5GiB" in _source(cgroup_v1)[1]


class TestCgroupV2:
    def test_full_v2_tree(self, cgroup_v2):
        code, limits = _source(cgroup_v2)
        assert code == 0
        assert limits == "CPU:2cores Memory:4GiB Processes:unlimited"

    def test_fractional_cpu(self, cgroup_v2):
        (cgroup_v2 / "cpu.max").write_text("50000 100000\n")
        assert _source(cgroup_v2)[1].startswith("CPU:0.5cores ")

    def test_max_quota_is_unlimited_cpu(self, cgroup_v2):
        (cgroup_v2 / "cpu.max").write_text("max 100000\n")
        assert _source(cgroup_v2)[1].startswith("CPU:unlimited ")

    def test_max_memory_is_unlimited(self, cgroup_v2):
        (cgroup_v2 / "memory.max").write_text("max\n")
        assert "Memory:unlimited" in _source(cgroup_v2)[1]


class TestDegradedRoots:
    """A cgroup the shell cannot read must degrade, never break the shell."""

    def test_empty_root_reports_unknown(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        code, limits = _source(empty)
        assert code == 0
        assert limits == "CPU:unknown Memory:unknown Processes:unknown"

    def test_missing_root_reports_unknown(self, tmp_path):
        code, limits = _source(tmp_path / "absent")
        assert code == 0
        assert limits == "CPU:unknown Memory:unknown Processes:unknown"

    def test_missing_root_under_errexit_and_nounset(self, tmp_path):
        """~/.bashrc may be sourced by a shell with set -e/-u active."""
        code, limits = _source(tmp_path / "absent", shell_opts="set -eu; ")
        assert code == 0
        assert limits == "CPU:unknown Memory:unknown Processes:unknown"

    def test_inherited_positionals_are_read_as_the_root(self, cgroup_v1):
        """Why the Containerfile passes the cgroup root explicitly.

        A sourced script with no arguments of its own inherits the sourcing
        shell's positional parameters, so a stray $1 becomes the cgroup root.
        Sourcing with an explicit root is what keeps that out of the shell.
        """
        result = subprocess.run(
            ["bash", "-c", '. "$1" && printf %s "$CONTAINER_LIMITS"',
             "bash", str(SCRIPT), str(cgroup_v1)],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "CPU:unknown Memory:unknown Processes:unknown"

    def test_garbage_values_report_unknown(self, tmp_path):
        garbage = _write_tree(tmp_path / "garbage", {
            "cpu.max": "nonsense\n",
            "memory.max": "nonsense\n",
            "pids.max": "nonsense\n",
        })
        assert _source(garbage)[1] == "CPU:unknown Memory:unknown Processes:unknown"


def test_sourcing_leaves_no_helper_functions_behind(cgroup_v1):
    """The helpers are internal; a session shell must not inherit them."""
    result = subprocess.run(
        ["bash", "-c", '. "$1" "$2" && declare -F | grep __lmer_limits || true',
         "bash", str(SCRIPT), str(cgroup_v1)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"helper functions leaked: {result.stdout}"


def test_the_value_reaches_child_processes(cgroup_v1):
    """The readers are the commands the session shell runs, not the shell.

    Every other test reads the variable in the sourcing shell, where a plain
    assignment and an export look alike. The env is replaced rather than
    extended because the ambient container env already carries an exported
    CONTAINER_LIMITS, which would satisfy the child on its own.
    """
    read_in_child = '. "$1" "$2"; bash -c \'printf %s "$CONTAINER_LIMITS"\''
    result = subprocess.run(
        ["bash", "-c", read_in_child, "bash", str(SCRIPT), str(cgroup_v1)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "CPU:1core Memory:2GiB Processes:32000"


def test_executing_prints_the_value(cgroup_v1):
    """Run rather than sourced, the script is a one-shot diagnostic."""
    result = subprocess.run(
        ["bash", str(SCRIPT), str(cgroup_v1)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "CPU:1core Memory:2GiB Processes:32000\n"
    assert result.stderr == ""


def test_sourcing_is_silent(cgroup_v1):
    """Sourced from ~/.bashrc, the script must not print anything itself."""
    result = subprocess.run(
        ["bash", "-c", '. "$1" "$2"', "bash", str(SCRIPT), str(cgroup_v1)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
