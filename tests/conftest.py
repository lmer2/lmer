"""Shared fixtures for tests."""
import ast
import contextlib
import errno
import inspect
import os
import subprocess
import textwrap
from pathlib import Path
from unittest import mock
import pytest

from lmer_cli import gate_lock, user_harnesses


#: Set on any host that is expected to be able to run the tests which *execute*
#: web sources under Node — see :func:`require_node_toolchain` for what changes
#: and why it is the host's call rather than the test's.
REQUIRE_NODE_ENV = "LMER_TESTS_REQUIRE_NODE"

#: Spellings of "yes". Anything else — an empty value included — leaves the
#: default in place, so a stray ``LMER_TESTS_REQUIRE_NODE=`` in a .env file does
#: not arm the strict mode by accident.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: LMER_* vars :func:`strip_lmer_env` leaves alone: switches that tell the *test
#: harness* how strict to be, which are not run context. Stripping one would let
#: any module that isolates its environment silently disarm a guard.
#:
#: ``LMER_GATE_LOCK_DIR`` is here for exactly that reason (issue #201): the
#: session fixture below points it at a tmp dir so the suite cannot see the
#: marker held by the very `gate-check` running it. Stripping it would send any
#: env-isolating module back to the operational lock dir, where a live gate
#: would defer the commit paths those tests are asserting on.
_HARNESS_ENV = frozenset({REQUIRE_NODE_ENV, gate_lock.LOCK_DIR_ENV})


def strip_lmer_env(monkeypatch):
    """Remove every LMER_* env var from the environment.

    Shared body of the per-module autouse ``_clean_lmer_env`` fixtures:
    with LMER_REPO_HOST/LMER_REPO_PROJECT unset, code reaching the `work`
    CLI has no run context and cannot write into the operational work repo
    (issue #93). Kept as a plain helper — an autouse fixture here would
    force stripping onto every test in the suite; modules opt in instead.

    :data:`_HARNESS_ENV` is exempt: those name how the suite should behave, not
    which run it belongs to, so removing them cannot help the isolation this is
    for and would quietly weaken the guards that read them.
    """
    for key in list(os.environ):
        if key.startswith("LMER_") and key not in _HARNESS_ENV:
            monkeypatch.delenv(key, raising=False)


def node_is_expected():
    """Whether a missing Node toolchain is a failure rather than a skip."""
    return os.environ.get(REQUIRE_NODE_ENV, "").strip().lower() in _TRUTHY


def require_node_toolchain(reason):
    """Skip the calling test — or fail it, on a host that is supposed to have Node.

    A few web guards do not read the sources, they *execute* them: the render
    path is lifted verbatim out of ``Markdown.vue`` and hostile input is run
    through it under Node (:mod:`tests.test_platform_web_markdown`). That is the
    only place the parser's own defences are exercised rather than pinned by
    reading, so it is the coverage least affordable to lose.

    And losing it was silent. With no Node those tests skipped; ``pytest -q``
    prints a skip as an ``s``, the run still exits 0, and nothing anywhere says
    that the strongest checks in the file did not run — the same shape as a
    vacuous test, which reads as passing while having verified nothing. It also
    fools any tool that judges a run by its exit code: a mutation sweep over the
    render path scored every one of those probes as "not caught" when they had
    simply never executed.

    So the host decides and the test obeys. A machine that can build the UI has
    a Node — ``lmer platform setup-ui`` fetches a pinned one into
    ``~/.lmer/platform/node/`` — and setting this variable is how such a machine
    says so. Someone on a Node-less laptop sets nothing, still gets a runnable
    suite, and is told in the skip reason what it cost.

    Under the flag this never skips: a guard that can be satisfied by skipping is
    the bug it was written to catch.
    """
    if node_is_expected():
        pytest.fail(
            f"{reason}\n"
            f"{REQUIRE_NODE_ENV} is set, so this host is expected to run the "
            "web guards that execute their sources. Skipping them here would "
            "drop the only coverage that runs hostile input through the real "
            "render path, and would leave the run green while doing it.\n"
            "To get a toolchain: `lmer platform setup-ui` fetches the pinned "
            "Node into ~/.lmer/platform/node/ and installs web/'s pinned JS "
            "dependencies. With a Node already on PATH, `npm ci` in web/ does "
            "the second half on its own.\n"
            f"To go back to skipping, unset {REQUIRE_NODE_ENV} — and accept "
            "that these guards did not run.",
            pytrace=False,
        )
    pytest.skip(
        f"{reason} — the web guards that execute their sources did not run; "
        f"set {REQUIRE_NODE_ENV}=1 to make this a failure instead"
    )


def node_binary():
    """The platform's own pinned Node if ``setup-ui`` has run, else one on PATH.

    Lives here because five test modules need it and every one of them used to
    carry its own copy — and two of those copies had only the first root, which is
    the one that never resolves inside pytest.

    Two roots, for that reason. ``node_dir()`` resolves through
    ``store.platform_dir()``, and :func:`_isolate_platform_state` repoints that at
    a tmp dir for the whole session, so the "pinned toolchain" branch was dead in
    every pytest invocation: a host whose only Node was the one
    ``lmer platform setup-ui`` fetched skipped every test that executes web
    sources, silently. The second root is the same path with the redirection
    undone — computing it is a path lookup, not a read of the state the fixture
    exists to isolate.

    Returns the path as a string, or ``None`` when there is no Node at all, which
    is :func:`require_node_toolchain`'s decision to act on rather than this
    function's.

    Calls :func:`tests.webdeps.ensure_web_deps` first, for the reason that module
    documents: Node's presence and ``web/node_modules``' presence are independent,
    and every session container has the former while a fresh checkout never has
    the latter. Each per-module copy of this resolver used to make the call; the
    chokepoint moved here with the resolver, so it is still the one place a
    node-using test passes through.
    """
    import shutil

    from tests.webdeps import ensure_web_deps

    ensure_web_deps()

    from lmer_cli.runtime import lmer_state_dir
    from lmer_platform.store import PLATFORM_DIRNAME, platform_dir
    from lmer_platform.ui_build import NodeToolchain, node_dir

    isolated = node_dir()
    # Derived from node_dir() rather than respelled, so a change to the layout
    # underneath the platform dir travels here instead of going quiet.
    installed = (
        lmer_state_dir() / PLATFORM_DIRNAME / isolated.relative_to(platform_dir())
    )
    for root in (isolated, installed):
        own = NodeToolchain(root=root)
        if own.node.is_file():
            return str(own.node)
    return shutil.which("node")


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


def _work_repo_head(work_repo_path):
    """The work repo's HEAD sha, or None when it cannot be read.

    Snapshotted alongside the porcelain status so the guard can tell its two
    causes apart (issue #201): a HEAD that moved during the run means a
    CONCURRENT WRITER committed — a `work commit` landing inside the gate's
    window — while a HEAD that stayed put with entries appearing points at the
    suite itself. None simply drops the extra diagnosis; no toplevel check is
    needed here because the guard consults this only once the status snapshot
    has already established a real work repo at this path.
    """
    work_repo = Path(work_repo_path)
    if not work_repo.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(work_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def work_repo_drift_report(
    work_repo_path, appeared, vanished, head_before=None, head_after=None
):
    """The leak guard's failure text, or None when nothing drifted.

    Pure assembly, kept out of the fixture so the message itself is testable —
    it is the part a person actually reads at 2am, and it used to send them to
    `LMER_*` env isolation and issue #93 for a cause that was neither.
    """
    if not appeared and not vanished:
        return None
    sections = []
    if appeared:
        sections.append("appeared:\n" + "\n".join(f"  {line}" for line in appeared))
    if vanished:
        sections.append(
            "vanished (deleted, or swept into a commit):\n"
            + "\n".join(f"  {line}" for line in vanished)
        )
    report = (
        f"Test suite leaked into or altered the operational work repo ({work_repo_path}):\n"
        + "\n".join(sections)
    )
    moved = bool(head_before and head_after and head_before != head_after)
    if moved:
        report += (
            f"\n\nHEAD MOVED during this run ({head_before[:8]} -> {head_after[:8]}): "
            "a concurrent writer COMMITTED while the suite was running. That is "
            "issue #201 — the session's own `work commit` (or an implicit push "
            "behind `work state set` / `goal` / `artifact`) landing inside a "
            "background gate's window — and not test leakage, so the LMER_* env "
            "isolation below is the wrong end to start from.\n"
            "Gate commands hold an in-flight marker (lmer_cli.gate_lock) and "
            "every work-repo durability commit defers while one is live, so this "
            "is supposed to be impossible: seeing it means THE DEFERRAL BROKE — a "
            "marker never written, one that expired early, or a write path that "
            "does not route through the check in "
            "work_repo.git_ops.commit_work_path. Chase that, not the tests."
        )
    elif appeared:
        report += (
            "\n\nHEAD did not move, so nothing was committed underneath the run. "
            "Entries can still appear without a commit: a plain work-repo write "
            "during the run (`work log`, `work event`) dirties a tracked file and "
            "shows up here (issue #201 — commits defer around a gate, bare writes "
            "do not)."
        )
    report += (
        "\nTests reaching the `work` CLI must isolate LMER_* env "
        "(see issue #93 and the _clean_lmer_env fixtures) — or a "
        "concurrent writer changed the work repo mid-run."
    )
    return report


@pytest.fixture(autouse=True, scope="session")
def _isolate_gate_lock_dir(tmp_path_factory):
    """Point gate-in-flight markers away from the operational lock dir (#201).

    `gate-check` holds a marker for its whole run — and the suite runs *inside*
    it. Without this, every test that exercises a work-repo commit path would
    see a live gate and defer instead of committing, i.e. the suite could only
    pass when nobody was gating it. Same floor-beneath-the-fixtures role as
    `_isolate_platform_state`; tests that want to see a marker write their own
    into this dir.

    BOTH the env var and the module default are redirected, and the second one
    is what actually holds: a test isolating its environment with
    `patch.dict(os.environ, …, clear=True)` drops the variable and would fall
    back to the operational `/tmp` dir — which is exactly how this fixture was
    first found wanting, by a napkin-commit test that deferred instead of
    staging while the gate ran it.
    """
    directory = str(tmp_path_factory.mktemp("gate-locks"))
    original_default = gate_lock.DEFAULT_LOCK_DIR
    gate_lock.DEFAULT_LOCK_DIR = directory
    os.environ[gate_lock.LOCK_DIR_ENV] = directory
    yield
    gate_lock.DEFAULT_LOCK_DIR = original_default
    os.environ.pop(gate_lock.LOCK_DIR_ENV, None)


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
    head_before = _work_repo_head(work_repo_path)
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
    report = work_repo_drift_report(
        work_repo_path,
        sorted(after - before),
        sorted(before - after),
        head_before,
        _work_repo_head(work_repo_path),
    )
    if report:
        pytest.fail(report, pytrace=False)


@pytest.fixture(autouse=True, scope="session")
def _isolate_user_harnesses(tmp_path_factory):
    """Point user-harness resolution away from the real ~/.lmer/harnesses.

    The harness resolution helpers (get_harness, resolve_harness_*,
    harness_for_model, UnknownHarnessError) consult user-installed harness
    definitions (issue #132), and DEFAULT_HARNESSES_DIR is Path.home()-based
    — so a dev/CI machine with harnesses installed would otherwise change
    hint-resolution results and unknown-harness listings in unrelated tests
    (same spirit as the work-repo leak guard above). Tests that want user
    harnesses set LMER_HARNESSES_DIR / pass an explicit root themselves.
    """
    os.environ.pop(user_harnesses.HARNESSES_DIR_ENV, None)
    empty = tmp_path_factory.mktemp("no-user-harnesses")
    original = user_harnesses.DEFAULT_HARNESSES_DIR
    user_harnesses.DEFAULT_HARNESSES_DIR = empty
    yield
    user_harnesses.DEFAULT_HARNESSES_DIR = original


@pytest.fixture(autouse=True, scope="session")
def _isolate_platform_state(tmp_path_factory):
    """Point platform state away from the real ~/.lmer/platform (issue #141).

    ``lmer_platform.store.PLATFORM_DIR`` defaults to ``lmer_state_dir()/platform``,
    so any test that reaches the store without patching it writes to the
    developer's real state dir — config, the shared secret, the tracked-run index
    and the work-repo mirror clone all live there.

    This is not hypothetical: a test that called ``monkeypatch.undo()`` reverted
    its own fixture's ``PLATFORM_DIR`` patch mid-test and cloned a pytest tmp
    repository over the real mirror. Per-test fixtures still patch this to their
    own tmp dir; this session-scoped default is the floor beneath them, in the
    same spirit as the work-repo leak guard and user-harness isolation above.
    """
    from lmer_platform import store as platform_store

    original = platform_store.PLATFORM_DIR
    platform_store.PLATFORM_DIR = str(tmp_path_factory.mktemp("platform-state"))
    yield
    platform_store.PLATFORM_DIR = original


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


# --- refusals the kernel will not perform for you ---------------------------
#
# A test that makes a real mode bit and expects the next syscall to fail is a
# test about the *uid the suite runs as*: root is exempt from the permission
# check for owned files, so `chmod(0o000)` then `open()` succeeds for uid 0 and
# the `pytest.raises` never fires. CI's pytest job runs as root while every
# developer runs as themselves, which is the worst version of it — green for
# everyone who runs the suite the usual way, red in the pipeline, forever.
#
# So the refusal is injected at the syscall the production code actually makes,
# and the assertion is about the code's handling of EACCES rather than about the
# kernel's willingness to produce one. Same shape as
# tests/test_platform_transcripts.py::test_a_write_failure_leaves_the_original_intact_and_no_temp,
# which injects for the same reason: the failure cannot be built from the
# filesystem without also breaking the thing being measured.


def _same_file(candidate, target) -> bool:
    """Whether two paths name one file, tolerant of how each was spelled."""
    try:
        return Path(candidate).resolve() == Path(target).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


@contextlib.contextmanager
def denied_read(path):
    """Make opening *path* raise ``PermissionError``, at any uid.

    Patches :meth:`pathlib.Path.open`, which is what the transcript reader and
    the scrubber use. Every other path opens normally.
    """
    target = Path(path)
    real_open = Path.open

    def guarded(self, *args, **kwargs):
        if _same_file(self, target):
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    with mock.patch.object(Path, "open", guarded):
        yield


@contextlib.contextmanager
def denied_create(directory):
    """Make creating a file *directly inside* ``directory`` raise ``PermissionError``.

    Patches :func:`os.open`, which is where :func:`lmer_platform.store.write_json`
    lands (through ``_create_owner_only``) — so this is the unwritable-state-dir
    case as the code meets it, without depending on the kernel to enforce a mode
    bit against the uid running the suite.
    """
    target = Path(directory)
    real_open = os.open

    def guarded(path, *args, **kwargs):
        try:
            parent = Path(os.fsdecode(path)).parent
        except (TypeError, ValueError):
            parent = None
        if parent is not None and _same_file(parent, target):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    with mock.patch("os.open", guarded):
        yield


@contextlib.contextmanager
def denied_access(path):
    """Make :func:`os.access` answer ``False`` for *path*, at any uid.

    The ask channel asks that question directly (``protocol.resolve_channel_dir``)
    and it is the one syscall root does not lie about by accident: ``os.access``
    for uid 0 returns True for a directory it could not have written had it been
    anybody else, which is exactly the uid-mismatch case being modelled.
    """
    target = Path(path)
    real_access = os.access

    def guarded(candidate, mode, **kwargs):
        if _same_file(candidate, target):
            return False
        return real_access(candidate, mode, **kwargs)

    with mock.patch("os.access", guarded):
        yield
