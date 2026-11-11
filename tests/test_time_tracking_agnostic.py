"""Time tracking stays tracker-agnostic, and its settings stay documented.

lmer knows no tracker: a handler other than ``timesheet`` is defined in the
work repo and synced outside lmer (docs/TIME-TRACKING.md). The name of the
tracker the feature was first built for may appear in docs and in test
fixtures standing in for a work-repo handler, never in anything else that
ships: code, taskdef prose, hooks, prompts or agent files.

NOTE: this module reads docs/LMER-CLI.md, so it is declared in
`.lmer/gate-check.yaml`'s `tests.text_diff_subset`.
"""
import re
import subprocess
from pathlib import Path

import pytest

from lmer_platform import config as cfg

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWED = ("docs/", "tests/")
TRACKER = re.compile(rb"click[\s_.-]?up", re.IGNORECASE)


def _tracked(root):
    """What git tracks in *root*, staged files included: what a commit ships."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached"], cwd=root, capture_output=True, check=True,
    ).stdout.decode().split("\0")
    return sorted({name for name in listed if name})


def _shipped_files(root=REPO_ROOT):
    for name in _tracked(root):
        path = root / name
        if not name.startswith(ALLOWED) and path.is_file():
            yield path


def _naming_a_tracker(root=REPO_ROOT):
    return [
        path.relative_to(root).as_posix()
        for path in _shipped_files(root)
        if TRACKER.search(path.read_bytes()) or TRACKER.search(path.name.encode())
    ]


def test_the_scan_sees_everything_that_ships():
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _shipped_files()}
    assert "src/lmer_timesheet/handlers.py" in scanned
    assert "web/src/components/TimeTrackingBar.vue" in scanned
    assert "taskdef/orchestrate/instructions.txt" in scanned
    assert {name.split("/")[0] for name in scanned} >= {
        "src", "web", "taskdef", "hooks", "bin", "libexec", "prompts", "agent-files",
    }
    allowed = {"docs/TIME-TRACKING.md", "tests/test_time_tracking_agnostic.py"}
    assert allowed <= set(_tracked(REPO_ROOT)) and not allowed & scanned


@pytest.mark.parametrize("spelling", [
    b"clickup", b"ClickUp", b"click_up", b"click-up", b"Click Up", b"click.up", b"api.clickup.com",
])
def test_the_pattern_catches_every_spelling(spelling):
    assert TRACKER.search(spelling)


def test_a_staged_file_fails_the_scan_and_an_untracked_note_does_not(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, text in {
        "taskdef/probe.txt": b"sync to Click-Up\n",
        "bin/clickup-sync": b"#!/bin/sh\n",
        "taskdef/clean.txt": b"nothing to see\n",
        "docs/guide.md": b"a ClickUp handler, as a work-repo example\n",
    }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(text)
    subprocess.run(["git", "add", "taskdef", "bin", "docs"], cwd=tmp_path, check=True)
    (tmp_path / "review-notes.md").write_bytes(b"checked the clickup handler\n")
    assert _naming_a_tracker(tmp_path) == ["bin/clickup-sync", "taskdef/probe.txt"]


def test_no_shipped_file_names_a_tracker():
    named = _naming_a_tracker()
    assert named == [], (
        "lmer ships no tracker-specific code; a tracker is a work-repo handler "
        f"definition plus a sync process outside lmer: {named}"
    )


#: Where time tracking resolves field values: lmer fetches nothing from a forge
#: or a tracker to fill one (#665 D25).
FETCH_FREE = ("src/lmer_timesheet/", "src/lmer_platform/timetrack", "src/lmer_platform/timesheet_handlers.py")
HTTP_CLIENT = re.compile(
    rb"^\s*(?:import|from)\s+(?:requests|httpx|urllib\.request|urllib3|aiohttp|http\.client)\b", re.M)


@pytest.mark.parametrize("line", [
    b"import requests", b"from httpx import Client", b"  import urllib.request", b"from http.client import X",
])
def test_the_http_client_pattern_catches_an_import(line):
    assert HTTP_CLIENT.search(line)


def test_time_tracking_fetches_nothing_to_fill_a_field():
    scanned = [name for name in _tracked(REPO_ROOT) if name.startswith(FETCH_FREE) and name.endswith(".py")]
    assert "src/lmer_platform/timetrack_runs.py" in scanned and "src/lmer_timesheet/issues.py" in scanned
    fetching = [name for name in scanned if HTTP_CLIENT.search((REPO_ROOT / name).read_bytes())]
    assert fetching == [], f"time tracking must not fetch to fill a field: {fetching}"


def _time_env_vars():
    return sorted({
        value for name, value in vars(cfg).items()
        if name.isupper() and isinstance(value, str) and value.startswith("LMER_PLATFORM_TIME_")
    })


def test_the_time_settings_are_found():
    names = _time_env_vars()
    assert cfg.ENV_TIME_WRITER_CREDENTIAL in names
    assert {env for _field, env in cfg.TIME_SETTING_KEYS.values()} <= set(names)


@pytest.mark.parametrize("name", _time_env_vars())
def test_every_time_setting_is_documented_where_every_other_one_is(name):
    doc = (REPO_ROOT / "docs" / "LMER-CLI.md").read_text(encoding="utf-8")
    assert name in doc
