"""Resource eligibility for a project's optional parallel test command."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
from typing import Optional


SERIAL_ENV = "LMER_GATE_NO_PARALLEL"


@dataclass(frozen=True)
class Limits:
    cpus: Optional[int]
    memory_bytes: Optional[int]


@dataclass(frozen=True)
class Selection:
    parallel: bool
    reason: str


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def _hierarchy(controller: str, proc: Path) -> tuple[list[Path], bool, bool]:
    membership = {}
    for line in (proc / "self/cgroup").read_text().splitlines():
        _, controllers, name = line.split(":", 2)
        for item in controllers.split(","):
            membership[item] = PurePosixPath(name)
    key = controller if controller in membership else ""
    group = membership[key]
    if not group.is_absolute() or ".." in group.parts:
        raise ValueError("invalid cgroup membership")
    candidates = []
    for line in (proc / "self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, mount = left.split(), right.split()
        if key:
            matches = mount[0] == "cgroup" and key in mount[2].split(",")
        else:
            matches = mount[0] == "cgroup2"
        if not matches:
            continue
        root = PurePosixPath(_unescape(fields[3]))
        if not group.is_relative_to(root):
            continue
        boundary = Path(_unescape(fields[4]))
        if not boundary.is_absolute() or ".." in boundary.parts:
            raise ValueError("invalid cgroup mount")
        current = boundary / group.relative_to(root)
        candidates.append((len(root.parts), current, boundary, root == PurePosixPath("/")))
    _, current, boundary, hierarchy_root = min(candidates, key=lambda candidate: candidate[0])
    directories = [current]
    while current != boundary:
        current = current.parent
        directories.append(current)
    return directories, key == "", hierarchy_root


def _limit(controller: str, proc: Path) -> Optional[int]:
    directories, unified, hierarchy_root = _hierarchy(controller, proc)
    values = []
    for index, directory in enumerate(directories):
        if controller == "cpu":
            filename = "cpu.max" if unified else "cpu.cfs_quota_us"
        else:
            filename = "memory.max" if unified else "memory.limit_in_bytes"
        path = directory / filename
        # The real cgroup v2 hierarchy root has no resource-limit files.
        if (unified and hierarchy_root and index and directory == directories[-1]
                and not path.exists()):
            continue
        raw = path.read_text().strip()
        if controller == "cpu":
            if unified:
                quota, period = raw.split()
            else:
                quota = raw
                period = (directory / "cpu.cfs_period_us").read_text().strip()
            period_value = int(period)
            if period_value <= 0:
                raise ValueError("invalid CPU period")
            if quota == ("max" if unified else "-1"):
                continue
            quota_value = int(quota)
            if quota_value <= 0:
                raise ValueError("invalid CPU quota")
            values.append(quota_value // period_value)
        elif not (unified and raw == "max"):
            value = int(raw)
            if value <= 0:
                raise ValueError("invalid memory limit")
            # v1 represents unlimited memory with a page-rounded LONG_MAX.
            if value < 1 << 60:
                values.append(value)
    return min(values) if values else None


def read_limits(proc: Path = Path("/proc")) -> Limits:
    """Read finite cgroup limits, including visible ancestors and affinity.

    Unknown limits remain unknown; host core counts and display strings cannot
    establish the resource allocation of a container.
    """
    try:
        cpus = _limit("cpu", proc)
        affinity = len(os.sched_getaffinity(0))
        cpus = min(cpus, affinity) if cpus is not None and affinity else None
    except (OSError, ValueError, KeyError, IndexError, AttributeError):
        cpus = None
    try:
        memory = _limit("memory", proc)
    except (OSError, ValueError, KeyError, IndexError):
        memory = None
    return Limits(cpus, memory)


def select_parallel(config: object) -> Selection:
    """Check a project's requirements without interpreting its test command."""
    if os.environ.get(SERIAL_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return Selection(False, f"disabled by {SERIAL_ENV}")
    if config is False:
        return Selection(False, "disabled by work-repo policy")
    required = {"min_cpus", "min_memory_mib"}
    if (not isinstance(config, dict) or not required <= set(config)
            or set(config) - required - {"protocol"}
            or config.get("protocol", "opaque") not in ("opaque", "argv-v1")):
        return Selection(False, "invalid tests.parallel configuration")
    if any(type(config[key]) is not int or config[key] <= 0
           for key in ("min_cpus", "min_memory_mib")):
        return Selection(False, "parallel resource requirements must be positive integers")
    limits = read_limits()
    if limits.cpus is None or limits.memory_bytes is None:
        return Selection(False, "finite cgroup limits or CPU affinity unavailable")
    if limits.cpus < config["min_cpus"]:
        return Selection(False, "CPU allocation below configured minimum")
    if limits.memory_bytes < config["min_memory_mib"] * 1024**2:
        return Selection(False, "memory allocation below configured minimum")
    return Selection(True, "verified CPU and memory allocation meets project requirements")
