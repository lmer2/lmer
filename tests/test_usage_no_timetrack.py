"""Usage tracking does not depend on human time tracking (#647).

No usage module may import, or name a path into, ``timetrack*``,
``lmer_timesheet`` or ``timesheet.d``. Modules are found by glob, so a usage
module added later is scanned without editing this list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Each pattern names where usage code lives; every package that exists must
#: contribute at least one module, so a rename cannot silently empty the scan.
USAGE_GLOBS = (
    "src/lmer_usage/*.py",
    "src/work_repo/usage.py",
    "src/lmer_platform/usage_*.py",
)
FORBIDDEN = re.compile(r"timetrack|lmer_timesheet|timesheet\.d|timesheet_d", re.IGNORECASE)


def usage_modules(root: Path = ROOT) -> list:
    found = []
    for pattern in USAGE_GLOBS:
        found.extend(sorted(root.glob(pattern)))
    return found


def offending_lines(path: Path) -> list:
    return [
        f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line)
    ]


def test_the_usage_core_is_scanned():
    assert any(ROOT.glob("src/lmer_usage/*.py")), "the usage core moved; update USAGE_GLOBS"


def test_spawn_harness_capture_is_scanned_once_it_exists():
    """The fan-out capture lives in spawn_harness.py; scan the usage half of it."""
    spawn = ROOT / "src" / "lmer_cli" / "container" / "spawn_harness.py"
    assert spawn.is_file()
    assert not offending_lines(spawn)


@pytest.mark.parametrize("module", usage_modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_usage_module_has_no_time_tracking_reference(module):
    assert not offending_lines(module)


@pytest.mark.parametrize("line", [
    "from lmer_timesheet import entries",
    "import timetrack_core",
    "path = run_dir / 'timesheet.d' / 'today.yaml'",
    "TIMETRACK_DIR = '~/.timetrack'",
])
def test_a_planted_reference_is_caught(tmp_path, line):
    planted = tmp_path / "usage_planted.py"
    planted.write_text(f'"""planted"""\n{line}\n')
    assert offending_lines(planted)
