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

_OUTDATED_TREE_MSG = (
    "tests/conftest.py needs the #233 write-attribution layer "
    "(work_repo.write_journal and gate_lock journal-only markers), but this "
    "process resolved lmer from a tree that predates it. On self-dev "
    "containers a bare `pytest` imports the OPERATIONAL install at "
    "/Agents/global/src rather than this checkout (issue #198) — an outdated "
    "image, not a broken checkout. Run the suite through gate-check (it "
    "prefixes PYTHONPATH with the checkout's src/), or invoke "
    "`PYTHONPATH=<checkout>/src pytest` yourself."
)

try:
    from work_repo import write_journal
except ImportError as exc:  # pragma: no cover — needs a pre-#233 install
    raise ImportError(_OUTDATED_TREE_MSG) from exc

if "journal_only" not in inspect.signature(gate_lock.hold_gate_lock).parameters:
    # Same skew, other module: write_journal may exist while gate_lock is
    # still the pre-#233 build (or vice versa) depending on how the ambient
    # tree was assembled — without this the failure is a TypeError deep in
    # fixture setup instead of an explanation.
    raise ImportError(_OUTDATED_TREE_MSG)


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

    `core.quotePath=false` keeps non-ASCII paths literal instead of
    quoted-and-escaped, which keeps accented session artifacts attributable
    (issue #233); git still C-quotes paths containing spaces, quotes,
    backslashes or control characters regardless of the setting, and
    porcelain_entry_path decodes those rather than refusing them.

    `errors="surrogateescape"` is what keeps a filename from disarming the
    guard entirely: with quotePath=false git emits raw bytes, so one
    non-UTF-8 name would otherwise raise UnicodeDecodeError, land in the
    except below, and return None — reported at suite START that means "no
    work repo to guard", i.e. the whole run silently skips, blame path
    included. Every other degradation here fails loud; this one would not
    (MR !200 review).

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
            ["git", "-C", str(work_repo), "-c", "core.quotePath=false",
             "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            errors="surrogateescape",
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
    work_repo_path,
    appeared,
    vanished,
    head_before=None,
    head_after=None,
    attributed=None,
):
    """The leak guard's failure text, or None when nothing drifted.

    Pure assembly, kept out of the fixture so the message itself is testable —
    it is the part a person actually reads at 2am, and it used to send them to
    `LMER_*` env isolation and issue #93 for a cause that was neither.

    *attributed* lists status entries the guard excluded as the session's own
    journaled writes (issue #233); they are appended for completeness so the
    reader sees the whole picture the guard saw, clearly separated from the
    drift that is actually failing the run.
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
    if attributed:
        report += (
            "\n\nExcluded from this verdict — journaled writes by this "
            "session's own work CLI during the run (issue #233):\n"
            + "\n".join(f"  {line}" for line in attributed)
        )
    return report


_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
              "\\": 92, '"': 34}


def unquote_c_style(quoted):
    """Decode git's C-style quoting, or None when the form is unrecognized.

    Git quotes any path containing a space, a double quote, a backslash or a
    control character — `core.quotePath=false` only stops it escaping
    non-ASCII — so refusing quoted entries outright would fail a whole run
    over a session artifact named `meeting notes.md`. The encoding is
    unambiguous and reversible, so decoding it is strictly better than
    refusing it; anything that does not decode still returns None and fails
    safe — including an octal escape above \\377, which git's own
    `quote_c_style` cannot emit (it formats an unsigned char) but which must
    not become a ValueError escaping through a teardown fixture as a raw
    ERROR instead of a verdict.
    """
    if len(quoted) < 2 or not quoted.startswith('"') or not quoted.endswith('"'):
        return None
    body = quoted[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8", "surrogateescape"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            return None
        nxt = body[index]
        if nxt in _C_ESCAPES:
            out.append(_C_ESCAPES[nxt])
            index += 1
        elif nxt in "01234567":
            octal = body[index:index + 3]
            if len(octal) < 3 or any(c not in "01234567" for c in octal):
                return None
            value = int(octal, 8)
            if value > 0xFF:
                return None
            out.append(value)
            index += 3
        else:
            return None
    return out.decode("utf-8", "surrogateescape")


def porcelain_status_code(line):
    """The two-character XY status field, or None when the line is unreadable.

    The status code is what says a path was DELETED (`D` in either column);
    the diff side a line lands on does not — a deletion arrives as a new
    ` D path` line, i.e. on the appeared side (issue #233, MR !200 review).
    """
    if len(line) < 4 or line[2] != " ":
        return None
    return line[:2]


def porcelain_entry_path(line):
    """The path a porcelain-v1 status line names, or None when it cannot be
    read with confidence. None routes the entry to "unattributed", which
    fails the run — the safe direction.

    Quoted paths are decoded (see unquote_c_style). Rename/copy lines
    (`old -> new`) still refuse, since the entry names two paths and the
    excusal machinery is built on one — but only for the `R`/`C` statuses
    that actually carry two-path syntax, so an ordinary untracked file whose
    NAME contains ` -> ` stays attributable (MR !200 review round 3).
    """
    if len(line) < 4 or line[2] != " ":
        return None
    code = line[:2]
    path = line[3:]
    if ("R" in code or "C" in code) and " -> " in path:
        return None
    if path.startswith('"'):
        if len(path) < 2 or not path.endswith('"'):
            return None
        return unquote_c_style(path)
    return path


def _locate_in_prefixes(path, prefixes):
    """(prefix, tail) for the declared prefix containing *path*, else None."""
    for prefix in prefixes:
        if path == prefix:
            return prefix, ""
        if path.startswith(prefix + "/"):
            return prefix, path[len(prefix) + 1:]
    return None


def partition_attributed_drift(appeared, vanished, attributed_scopes):
    """Split drift into (attributed, residual_appeared, residual_vanished).

    *attributed_scopes* is one prefix list PER journaled record, not a
    flattened union: additions may be excused by any declared prefix (the
    accepted scope granularity), but a removal's rename counterpart must come
    from the SAME record, because "these two addresses belong to one run" is
    exactly what a single record asserts and a union does not. Flattened, a
    `work seed` of an unrelated run supplied a fresh `?? other/state.yaml`
    that excused a test's ` D current/state.yaml` — the counterpart was
    fabricated from two unrelated scopes (MR !200 review round 3).

    An entry is excusable when it sits under a prefix some journaled
    non-ancestor `work` invocation declared as its write scope (issue #233) —
    with removals held to a stricter test, because a session write never
    destroys content and a test deleting run-dir files is the #93 shape the
    guard exists for.

    "Removal" is read from the STATUS CODE, not the diff side: deleting a
    tracked file adds a new ` D path` line, so it arrives among *appeared*
    entries, while *vanished* means a status line stopped being reported —
    which happens when its file was removed **or renamed away**. Keying on
    the diff side alone gets both wrong (MR !200 review, major finding).

    The session does vacate paths legitimately: `work goals freeze` and the
    lazy rename behind `work name` move the whole run dir, which shows up as
    ` D old/...` appearing, `?? new/...` appearing, and the old dir's former
    entries vanishing. What separates that from a test's `rmtree` is a fact
    already in the data — after a rename the journal declares BOTH addresses
    in one record (`run_rel_path_candidates`), so every vacated path has a
    counterpart with the same tail landing under a *different* prefix of that
    same record. A removal with no such counterpart is never excused, and a
    record declaring one prefix (no rename in play) can excuse no removal at
    all.
    """
    scopes = [
        [prefix.rstrip("/") for prefix in scope if prefix]
        for scope in attributed_scopes
    ]
    scopes = [scope for scope in scopes if scope]
    all_prefixes = sorted({prefix for scope in scopes for prefix in scope})

    # Content present under a declared prefix at teardown — the landing half
    # of a rename. Removals cannot themselves be a landing.
    landed = set()
    for line in appeared:
        path = porcelain_entry_path(line)
        code = porcelain_status_code(line)
        if path is None or code is None or "D" in code:
            continue
        located = _locate_in_prefixes(path, all_prefixes)
        if located is not None:
            landed.add(located)

    def vacated_by_declared_rename(path):
        for scope in scopes:
            located = _locate_in_prefixes(path, scope)
            if located is None:
                continue
            prefix, tail = located
            if any(
                other != prefix and (other, tail) in landed for other in scope
            ):
                return True
        return False

    attributed, residual_appeared, residual_vanished = [], [], []
    for line in appeared:
        path = porcelain_entry_path(line)
        code = porcelain_status_code(line)
        if (
            path is None
            or code is None
            or _locate_in_prefixes(path, all_prefixes) is None
            or ("D" in code and not vacated_by_declared_rename(path))
        ):
            residual_appeared.append(line)
        else:
            attributed.append(line)
    for line in vanished:
        path = porcelain_entry_path(line)
        if path is not None and vacated_by_declared_rename(path):
            attributed.append(line)
        else:
            residual_vanished.append(line)
    return attributed, residual_appeared, residual_vanished


def work_repo_blame_report(work_repo_path, blamed, appeared, vanished):
    """Failure text when a journaled `work` write descends from this suite.

    The ancestry verdict is recorded by the `work` CLI itself at write time
    (issue #233), so unlike the drift report this one can NAME the leaking
    command — the reader starts at the test that spawned it, not at
    env-isolation archaeology. Pure, for the same 2am reason as
    work_repo_drift_report.
    """
    commands = ", ".join(
        f"`work {record.get('command')}` (pid {record.get('pid')})"
        for record in blamed
    )
    report = (
        f"A test-descendant `work` invocation reached the operational work "
        f"repo ({work_repo_path}) with write intent while this suite ran: "
        f"{commands}.\n"
        "The invocation journaled its own ancestry (issue #233): the writing "
        "process descends from this pytest process, so a test reached the "
        "real `work` CLI with ambient session env (issue #93). Isolate the "
        "test's LMER_* env (see the _clean_lmer_env fixtures)."
    )
    sections = []
    if appeared:
        sections.append("appeared:\n" + "\n".join(f"  {line}" for line in appeared))
    if vanished:
        sections.append("vanished:\n" + "\n".join(f"  {line}" for line in vanished))
    if sections:
        report += "\n\n" + "\n".join(sections)
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

    Three additions live here because this fixture is the one that knows both
    lock dirs (issue #233). It captures the OPERATIONAL dir before the
    redirect and (a) holds a gate marker for the pytest process there, so
    work-repo durability commits defer during bare `pytest` runs exactly as
    they do inside gate commands — outside `work` processes read that dir,
    not the redirected one; (b) holds a JOURNAL-ONLY marker in the redirected
    dir, because a leaking test's `work` child inherits this suite's
    environment — redirect included — and would otherwise see no marker and
    journal nothing, leaving its writes indistinguishable from the session's
    excused ones (journal-only does not defer, so the commit paths tests
    exercise keep committing); and (c) yields the captured operational dir,
    so the leak guard below can consume the write-attribution journal living
    beside the markers.
    """
    operational = gate_lock.lock_dir()
    directory = str(tmp_path_factory.mktemp("gate-locks"))
    original_default = gate_lock.DEFAULT_LOCK_DIR
    gate_lock.DEFAULT_LOCK_DIR = directory
    os.environ[gate_lock.LOCK_DIR_ENV] = directory
    try:
        with gate_lock.hold_gate_lock("pytest-suite", directory=operational):
            with gate_lock.hold_gate_lock(
                "pytest-suite", directory=Path(directory), journal_only=True
            ):
                yield operational
    finally:
        gate_lock.DEFAULT_LOCK_DIR = original_default
        os.environ.pop(gate_lock.LOCK_DIR_ENV, None)


@pytest.fixture(autouse=True, scope="session")
def _work_repo_leak_guard(_isolate_gate_lock_dir):
    """Fail the suite if tests leak run-state into the real work repo.

    Tests that reach the `work` CLI (e.g. via hooks.start's
    `work session-start` subprocess) with ambient session env have seeded,
    claimed, and mutated runs in the operational work repo (issue #93).
    This guard snapshots the repo's git status before the suite and fails
    at teardown — naming appeared AND vanished status entries, deleting
    nothing — if the suite changed it. A work repo that was snapshottable
    at suite start but not at teardown (e.g. deleted mid-suite) is also a
    failure. Skips only when no work repo is available to begin with.

    Drift is partitioned by attribution before the verdict (issue #233).
    The session that launched this suite keeps doing its job while it runs —
    `work log`, `work event`, the writes behind `work state set` — and those
    writes journal themselves (beside the gate markers, with a
    process-ancestry verdict recorded at write time). Entries under a
    non-ancestor invocation's declared write scope are excused; a journaled
    write that DESCENDS from this pytest process fails the run naming the
    command; unattributed drift and any HEAD move keep the hard failure
    above. Removals are held to a stricter test than additions — see
    partition_attributed_drift — so a test deleting run-dir content still
    fails while the session's own run-dir rename does not. The excuse is
    prefix-scoped, not per-file: a non-CLI write landing inside an excused
    prefix rides that excuse, which is the granularity trade-off documented
    at cli._write_intent_rel_paths.
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
    # Two journals feed the verdict: the OPERATIONAL lock dir (the launching
    # session's writes, and children whose cleared env fell back to the
    # default dir) and the REDIRECTED dir this suite runs against (children
    # that inherited the suite's environment journal against the journal-only
    # marker there — the #93 shape). This teardown runs before the lock-dir
    # fixture's, so gate_lock.lock_dir() still names the redirected dir.
    records = write_journal.consume_records(
        work_repo_path, os.getpid(), directory=_isolate_gate_lock_dir
    ) + write_journal.consume_records(work_repo_path, os.getpid())
    appeared = sorted(after - before)
    vanished = sorted(before - after)
    blamed = [
        record
        for record in records
        if write_journal.guard_verdict(record, os.getpid()) is True
    ]
    if blamed:
        pytest.fail(
            work_repo_blame_report(work_repo_path, blamed, appeared, vanished),
            pytrace=False,
        )
    head_after = _work_repo_head(work_repo_path)
    if head_before and head_after and head_before != head_after:
        # A moved HEAD is never excused: commits defer while this suite holds
        # its marker, so one landing anyway means the deferral broke — or one
        # of the two deliberate `allow_during_gate` committers ran (a release
        # CAS claim, or `work session-end` tearing this container down, in
        # which case this verdict is moot). Attribution must not soften the
        # signal either way.
        attributed, residual_appeared, residual_vanished = [], appeared, vanished
    else:
        # One scope per record — NOT a flattened union: which addresses were
        # declared together is what licenses a rename counterpart (#233).
        attributed_scopes = [
            record.get("rel_paths", [])
            for record in records
            if write_journal.guard_verdict(record, os.getpid()) is False
        ]
        attributed, residual_appeared, residual_vanished = (
            partition_attributed_drift(appeared, vanished, attributed_scopes)
        )
    report = work_repo_drift_report(
        work_repo_path,
        residual_appeared,
        residual_vanished,
        head_before,
        head_after,
        attributed=attributed,
    )
    if report:
        pytest.fail(report, pytrace=False)
    if attributed:
        print(
            f"\n[work-repo leak guard] {len(attributed)} status change(s) in "
            f"{work_repo_path} attributed to this session's own journaled "
            "work-CLI writes (issue #233):"
        )
        for line in attributed:
            print(f"  {line}")


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


@pytest.fixture(autouse=True, scope="session")
def _isolate_session_log_dir(tmp_path_factory):
    """Keep supervisor tests out of the *live* session log (#210).

    ``supervisor.CONTAINER_SESSION_LOG_DIR`` is a real path, and in a session
    that has one mounted it is the log the running supervisor is recording the
    operator's own terminal into — which the platform serves back as that
    session's terminal view. So every test that ran ``run_supervisor`` without
    redirecting it appended its wrapped child's *raw PTY traffic* to what the
    operator is looking at: 120 ``tick`` lines from the forwarding-loop test
    alone, plus ``/start`` injections, a ^C and the escape sequences that come
    with them, interleaved with the TUI mid-draw.

    Pointed at a path that does not exist, which is the supervisor's documented
    "nothing was mounted" case: no log is opened at all, so a test cannot leak
    through one however it fails or is interrupted. Tests that assert *on* the
    log point this at their own tmp dir, and their patch wins; this is the floor
    beneath them, like the platform-state and gate-lock isolation above.

    Only the *writer's* copy moves. ``lmer_platform.spawn`` imports the constant
    by value and uses it to build a container ``--mount-dir`` — it writes nothing
    locally, and the spawn tests bake the real value into parametrize at
    collection time, before this fixture runs. Redirecting that copy too would
    only make the suite assert against a divergence it created itself.
    """
    from lmer_cli import supervisor

    original = supervisor.CONTAINER_SESSION_LOG_DIR
    supervisor.CONTAINER_SESSION_LOG_DIR = str(
        tmp_path_factory.mktemp("session-log") / "not-mounted"
    )
    yield
    supervisor.CONTAINER_SESSION_LOG_DIR = original


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


# --- service slots (issue #245) ----------------------------------------------

#: The presets a slot fixture host knows — one entry per shape the slot rules
#: care about, in one place, because three modules carrying their own copy is
#: how the three drifted.
SLOT_PRESETS = {
    "webapp_dev": {"checkout": "/srv/webapp", "service": "webapp-web"},
    "other_dev": {"checkout": "/srv/other", "service": "other-web"},
    # Resolves to the same service as webapp_dev — the collision rule.
    "webapp_alt": {"checkout": "/srv/webapp", "service": "webapp-web"},
    # Sets no service, so it cannot back a slot.
    "no_service": {"checkout": "/srv/plain"},
    # Service groups (issue #312): a whole compose project, optionally with the
    # member the session starts on.
    "stack_dev": {"checkout": "/srv/stack", "service_group": "stack"},
    "stack_alt": {"checkout": "/srv/stack", "service_group": "stack"},
    "stack_start": {
        "checkout": "/srv/stack", "service": "web",
        "service_group": "stack",
    },
}

#: What :data:`SLOT_PRESETS`' group resolves to. ``webapp-web`` is deliberately
#: a member: a group session must block the single-service slot that names it.
SLOT_GROUP_MEMBERS = {
    "stack": ("web", "db", "webapp-web"),
}


@pytest.fixture
def slot_host(tmp_path, monkeypatch):
    """A host that knows :data:`SLOT_PRESETS`, with every service resolving.

    Yields the list of ``(runtime, service_name, announce)`` the probe was asked
    for, so a test can assert what was queried and what was not.

    The fake's signature is a deliberate copy of the real
    :func:`lmer_cli.service.resolve_container` — ``announce`` keyword-only. A
    laxer fake (one that also accepts it positionally) passes a caller that
    would fail against the real function, which is precisely the regression a
    fake is supposed to catch.
    """
    import json as _json

    from lmer_platform import slots as slots_mod

    path = tmp_path / "slot-presets.json"
    path.write_text(_json.dumps(SLOT_PRESETS), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))

    calls = []

    def _resolve(runtime, service_name, *, announce=True):
        calls.append((runtime, service_name, announce))
        return "container-id"

    def _resolve_group(runtime, project, *, announce=True):
        """The group half of the same fake, with the real signature (#312)."""
        from lmer_cli.service import ServiceError, ServiceMember

        calls.append((runtime, project, announce))
        members = SLOT_GROUP_MEMBERS.get(project)
        if not members:
            raise ServiceError(
                f"No running containers in compose project {project!r}"
            )
        return [
            ServiceMember(name, f"container-{name}", f"{project}-{name}-1", "/")
            for name in members
        ]

    monkeypatch.setattr(slots_mod, "resolve_container", _resolve)
    monkeypatch.setattr(slots_mod, "resolve_group", _resolve_group)
    monkeypatch.setattr(slots_mod, "_detected_runtime", lambda: "a-runtime")
    # Module-level memos (probe answers, collision-warning dedup): no test may
    # inherit another's.
    slots_mod.clear_probe_cache()
    yield calls
    slots_mod.clear_probe_cache()
