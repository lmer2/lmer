"""Shared fixtures for tests."""
import ast
import inspect
import os
import subprocess
import textwrap
from pathlib import Path
import pytest


def strip_lmer_env(monkeypatch):
    """Remove every LMER_* env var from the environment.

    Shared body of the per-module autouse ``_clean_lmer_env`` fixtures:
    with LMER_REPO_HOST/LMER_REPO_PROJECT unset, code reaching the `work`
    CLI has no run context and cannot write into the operational work repo
    (issue #93). Kept as a plain helper — an autouse fixture here would
    force stripping onto every test in the suite; modules opt in instead.
    """
    for key in list(os.environ):
        if key.startswith("LMER_"):
            monkeypatch.delenv(key, raising=False)


def ast_body_lines(fn):
    """Unparse a function's body, minus its docstring, one statement per line.

    Shared mechanic of the mirror-guard tests (hooks/start.py deliberately
    does not import lmer_cli, so a few functions are mirrored rather than
    shared): comparing two functions' ast_body_lines asserts their bodies are
    semantically identical while ignoring docstrings and formatting.
    """
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    func = tree.body[0]
    non_doc = [
        node
        for node in func.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    return [ast.unparse(node) for node in non_doc]


def _work_repo_status_lines(work_repo_path):
    """Snapshot the work repo's git status as a frozenset of porcelain lines.

    `--untracked-files=all` lists files inside untracked directories
    individually, so a new file appearing under an already-untracked run
    dir still changes the snapshot. (Appending content to an existing
    untracked or already-modified file leaves porcelain output unchanged —
    a known blind spot of any status-based diff.)

    Returns None when there is nothing to guard: the path is not a
    directory, git is unavailable, or the path is not itself the top level
    of a git repo (contributor machines and CI have no operational work
    repo; `git -C` would otherwise discover an enclosing repo and snapshot
    the wrong tree).
    """
    work_repo = Path(work_repo_path)
    if not work_repo.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(work_repo), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return None
        toplevel = subprocess.run(
            ["git", "-C", str(work_repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if toplevel.returncode != 0:
            return None
        if Path(toplevel.stdout.strip()).resolve() != work_repo.resolve():
            return None
    except Exception:
        return None
    return frozenset(
        line for line in result.stdout.splitlines() if line.strip()
    )


@pytest.fixture(autouse=True, scope="session")
def _work_repo_leak_guard():
    """Fail the suite if tests leak run-state into the real work repo.

    Tests that reach the `work` CLI (e.g. via hooks.start's
    `work session-start` subprocess) with ambient session env have seeded,
    claimed, and mutated runs in the operational work repo (issue #93).
    This guard snapshots the repo's git status before the suite and fails
    at teardown — naming appeared AND vanished status entries, deleting
    nothing — if the suite changed it. A work repo that was snapshottable
    at suite start but not at teardown (e.g. deleted mid-suite) is also a
    failure. Skips only when no work repo is available to begin with.
    """
    work_repo_path = os.environ.get("LMER_WORK_REPO_PATH", "/work")
    before = _work_repo_status_lines(work_repo_path)
    yield
    if before is None:
        return
    after = _work_repo_status_lines(work_repo_path)
    if after is None:
        pytest.fail(
            f"The operational work repo ({work_repo_path}) was "
            "snapshottable at suite start but not at teardown — a test "
            "deleted or broke it (issue #93).",
            pytrace=False,
        )
    appeared = sorted(after - before)
    vanished = sorted(before - after)
    if appeared or vanished:
        sections = []
        if appeared:
            sections.append(
                "appeared:\n" + "\n".join(f"  {line}" for line in appeared)
            )
        if vanished:
            sections.append(
                "vanished (deleted, or swept into a commit):\n"
                + "\n".join(f"  {line}" for line in vanished)
            )
        pytest.fail(
            f"Test suite leaked into or altered the operational work repo ({work_repo_path}):\n"
            + "\n".join(sections)
            + "\nTests reaching the `work` CLI must isolate LMER_* env "
            "(see issue #93 and the _clean_lmer_env fixtures) — or a "
            "concurrent writer changed the work repo mid-run.",
            pytrace=False,
        )


@pytest.fixture
def project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


@pytest.fixture
def rules_dir(project_root):
    """Get the rules directory."""
    return project_root / "rules"


@pytest.fixture
def clean_env(monkeypatch):
    """Fixture to track and clean environment variables."""
    original_env = os.environ.copy()

    # Track any env vars we set
    set_vars = set()

    original_setitem = monkeypatch.setenv

    def tracking_setenv(name, value):
        set_vars.add(name)
        original_setitem(name, value)

    monkeypatch.setenv = tracking_setenv

    yield monkeypatch

    # Verify no secrets were set
    for var in set_vars:
        assert not any(secret in var.upper() for secret in ['PASSWORD', 'TOKEN', 'KEY', 'SECRET']), \
            f"Potential secret in env var name: {var}"


@pytest.fixture
def all_rule_files(rules_dir):
    """Get all rule markdown files."""
    return list(rules_dir.glob("*.md"))


@pytest.fixture
def main_config(project_root):
    """Get the main AGENTS.md file."""
    return project_root / "AGENTS.md"


@pytest.fixture
def lmer_subprocess_env():
    """Env dict for tests that shell out to the `lmer` CLI.

    The CLI requires ``LMER_WORK_REPO`` early, before unrelated codepaths
    (e.g. .env loading) run. Tests that exercise those unrelated paths need a
    value present even when CI doesn't set one — this fixture supplies a dummy
    if the real one isn't already in the environment.
    """
    return {
        **os.environ,
        "LMER_WORK_REPO": os.environ.get(
            "LMER_WORK_REPO", "git@example.com:fixture/work-repo.git"
        ),
    }
