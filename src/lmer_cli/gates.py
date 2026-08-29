#!/usr/bin/env python3
"""
Gate System - Enforces all rules before major operations
This module provides static enforcement of all development rules.
"""

import subprocess
import sys
import os
import re
import json
import fnmatch
import textwrap
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from urllib.parse import urlsplit
from enum import Enum
import hashlib
import time

import yaml

from lmer_cli import gate_cache, precommit_cache, push_allow
from lmer_cli.container.clone_and_exec import _scrub_credentials
from lmer_cli.util import get_bool_env
from work_repo.utils import project_info_dir, redact_secrets, task_info_dir


class CheckStatus(Enum):
    """Status of a check"""
    PASSED = "✅ PASSED"
    FAILED = "❌ FAILED"
    WARNING = "⚠️  WARNING"
    SKIPPED = "⏭️  SKIPPED"


@dataclass
class CheckResult:
    """Result of a single check"""
    name: str
    status: CheckStatus
    message: str = ""
    details: List[str] = None
    is_critical: bool = True
    # Complete captured output for checks that shell out (tests, pre-commit).
    # `details` only holds a short tail for the terminal; `full_output` is the
    # untruncated stdout+stderr, persisted to the gate-check log so failures can
    # be investigated without re-running the slow checks.
    full_output: Optional[str] = None
    # Set when the check ran something other than the unqualified thing its
    # name implies: the text-diff test subset, a reused cached run, or the
    # suite a project's own runner declared it took (TEST_MODE_MARKER).
    # Receipts and logs read this, so a narrowed run can never be read back
    # as the full one, and a fallback suite can never be read back as the
    # project's usual one.
    scope: Optional[str] = None
    # The concrete targets the check selected and ran (the tests check's
    # pytest paths). None means the check chose no target list at all — a
    # project's own runner owns its invocation, and a missing tests directory
    # runs nothing — which the receipt records as "cannot say" rather than
    # as a full run.
    scope_targets: Optional[List[str]] = None
    # Structured cache decision for receipts. Reasons never carry environment
    # values; environment misses name variables only.
    cache_verdict: Optional[str] = None
    cache_reason: Optional[str] = None


@dataclass
class PassingSuite:
    """A cache pass eligible for a content-checked post-commit handoff."""
    argv: List[str]
    environment: Dict[str, str]
    summary: Optional[str]
    indexed_tree: str


# Fixed, predictable location for the full gate-check log. Overwritten on every
# run so it always reflects the latest invocation. Lives under /tmp so it is
# never committed and needs no per-project configuration.
GATE_CHECK_LOG_PATH = Path("/tmp/gate-check.log")

# A pytest tail line ("1397 passed in 42.1s", "1 failed, 12 passed in 3s",
# "no tests ran in 0.01s" is intentionally NOT matched — no count, no claim).
PYTEST_SUMMARY_RE = re.compile(
    r"\b\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)\b"
)

# The one line a project-supplied test runner may print to name the path it
# took (#307). A project whose suite normally runs inside a service container
# needs a fallback for sessions that have none, and a fallback nobody can see
# is indistinguishable from a bypass — so the runner has to be able to say
# which suite it ran. It SAYS it rather than lmer inferring it: gate-check runs
# that script as a black box and nothing about a shell script promises any
# particular output, so a marker lmer defines can only ever be absent, never
# wrong. Opt-in — a runner that prints nothing reports nothing, exactly as
# before.
TEST_MODE_MARKER = "gate-check: test-mode="

# Ceiling for a marker label, which is interpolated into a one-line result. A
# runner that accidentally pipes a whole log line through the marker gets
# truncated rather than owning the screen.
TEST_MODE_LABEL_MAX = 120

# Spec-class deliverable naming for check_deliverable_formats (#102): any path
# component with a word starting `spec`/`plan`/`report` (spec.docx, docs/specs/,
# project-plan.odt, reports/q2.pdf, specification.md). Leading word boundary
# only, so "inspect.pdf" or "replanted.pdf" do NOT match while
# "specification.docx" and "planning.pdf" do.
DELIVERABLE_NAME_RE = re.compile(r"\b(?:spec|plan|report)", re.IGNORECASE)

# Binary/office document extensions that make a spec-class deliverable
# unreviewable in GitLab (undiffable, unlinkable at line level).
BINARY_DOC_EXTENSIONS = {".docx", ".doc", ".pdf", ".odt", ".rtf"}

# Separator between the repo half and the ref half of an LMER_PUSH_ALLOW_LIST
# entry (`repo|refpattern`). `:` is unusable because the repo half is itself a
# URL fragment where `:` already appears (SSH remotes `git@host:group/proj`,
# `https://host:port`), and `,` is the entry separator — same reasoning as
# LMER_MOUNT_FILES picking a separator not claimed by its field grammar
# (cli.py parse_file_mount_specs). `|` appears in neither git URLs nor any
# refname in practice, so the split is unambiguous.
PUSH_ALLOW_REF_DELIMITER = "|"

# Repo-relative fnmatch patterns for paths that carry prose rather than
# behavior (#269). A diff touching only these can still fail the suite — this
# repo's own tests read docs and rule files — so matching them never skips
# tests; it selects the declared subset of tests that READ text
# (tests.text_diff_subset). fnmatch's `*` crosses `/`, so one `*.<ext>`
# pattern covers that extension at any depth; there are no depth-specific
# duplicates here for that reason.
#
# The list is global while the subset that compensates for it is per-repo, so
# a pattern is only carried when the name says prose in ANY project:
# - `*.txt` is not here: `requirements.txt` pins dependencies and
#   `taskdef/*/instructions.txt` is agent-facing behavior. `.txt` names a
#   file format, not a role.
# - Neither is a whole `docs/` subtree: `docs/conf.py` executes, and a
#   generator's data files live there too. Prose under `docs/` is already
#   covered by its extension; a project wanting more must widen this list
#   knowing every project pays for it.
TEXT_DIFF_PATTERNS = (
    "*.md", "*.rst",
    "changelog.d/*.yaml", "changelog.d/*.yml",
    "CHANGELOG.yaml", "LICENSE",
)

# Escape hatch for the above: forces the full suite regardless of the diff.
TEXT_DIFF_FASTPATH_OFF_ENV = "LMER_GATE_NO_FASTPATH"

# What `CheckResult.scope` says when the tests check ran the declared subset.
# Read by receipt_summary, so a receipt never presents a subset run as a
# full-suite pass.
TEXT_DIFF_SCOPE = "text-diff subset"

# What `CheckResult.scope` says when no suite ran at all because this exact
# tree was already proven green (lmer_cli.gate_cache). Prefixed to the scope
# of the run being reused ("cached full suite", "cached text-diff subset"),
# so both facts stay readable: nothing ran, and what it was that had run.
CACHE_SCOPE = "cached"

# The name of an unnarrowed run. `CheckResult.scope` stays None for a fresh
# full suite — there is nothing to disclaim on the terminal — so this is the
# word used wherever the full run has to be NAMED rather than implied: the
# cached scope ("cached full suite", never a bare "cached" that a reader has
# to know covers everything) and the receipt's `test_scope` field, where
# absence has to keep meaning "this run cannot say".
FULL_SUITE_SCOPE = "full suite"

# check_changelog() warning hints for repos with a changelog.d/ directory
CTL_FRAGMENT_HINT = "Or stage a fragment: changelog.d/YYYYMMDD-<topic>.yaml"
OTHER_TOOL_FRAGMENT_HINT = (
    "Or stage a changelog.d/ fragment in this repo's fragment convention"
)


def is_text_diff_path(path: str) -> bool:
    """True if `path` (repo-relative, posix) is prose by TEXT_DIFF_PATTERNS.

    Module-level so the gate and the guard test that keeps
    `tests.text_diff_subset` honest share one definition of "text".
    """
    candidate = path.strip()
    if not candidate:
        return False
    return any(fnmatch.fnmatch(candidate, pattern)
               for pattern in TEXT_DIFF_PATTERNS)


def pytest_summary_line(output: Optional[str]) -> Optional[str]:
    """The test runner's own tail line ("8727 passed, 40 skipped in 613.08s").

    The LAST match, since pytest's summary is the last thing it prints. None
    when the output carries no countable claim — receipts and cache entries
    then simply have no summary, never a fabricated one.
    """
    for line in reversed((output or "").splitlines()):
        line = line.strip().strip("=").strip()
        if PYTEST_SUMMARY_RE.search(line):
            return line
    return None


def runner_mode_label(output: Optional[str]) -> Optional[str]:
    """The path a custom test runner said it took, or None if it said nothing.

    Reads TEST_MODE_MARKER lines out of ONE captured stream. The LAST one
    wins, as in pytest_summary_line — and it is what lets a runner that
    delegates to another runner be described by the inner one that actually ran
    the suite. Pass a single stream: line order only means anything within one,
    so the caller reads stdout and stderr separately rather than handing this
    a concatenation whose interleaving was already lost.

    The label is sanitized on the way in rather than at the print site: it is
    interpolated into a line the gate prints as its own verdict, so a label
    carrying ANSI escapes or a newline could repaint that verdict.
    """
    for line in reversed((output or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith(TEST_MODE_MARKER):
            continue
        label = "".join(
            ch for ch in stripped[len(TEST_MODE_MARKER):] if ch.isprintable()
        ).strip()
        # An empty label is a runner that declared nothing, so keep looking
        # rather than reporting a mode of "".
        if not label:
            continue
        if len(label) > TEST_MODE_LABEL_MAX:
            label = label[:TEST_MODE_LABEL_MAX - 1].rstrip() + "…"
        return label
    return None


def _capture(result: subprocess.CompletedProcess) -> str:
    """Join and redact subprocess output before any gate sink can read it."""
    return redact_secrets((result.stdout or "") + (result.stderr or ""))


def receipt_argv() -> List[str]:
    """The invocation as run, with the interpreter-resolved path reduced to
    the command name (receipts record WHAT ran, not where it was installed).
    Shared by the gate bins so the argv normalization can't drift (#88)."""
    return [os.path.basename(sys.argv[0])] + sys.argv[1:]


class Colors:
    """Terminal colors for output"""
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[0;33m'
    BLUE = '\033[0;34m'
    MAGENTA = '\033[0;35m'
    CYAN = '\033[0;36m'
    WHITE = '\033[0;37m'
    BOLD = '\033[1m'
    NC = '\033[0m'  # No Color


class GateSystem:
    """Main gate system for enforcing development rules"""

    def __init__(self, verbose: bool = False, debug: bool = False,
                 commit_handoff: bool = False):
        self.verbose = verbose
        self.debug = debug
        self.commit_handoff = commit_handoff
        self.results: List[CheckResult] = []
        self.project_root = Path.cwd()
        self.timestamp = time.time()
        # Push-gate context for the text-diff fast path (#269). run_push_gate
        # sets these before delegating to run_commit_gate: what a push changes
        # is the commit range, not the index, so the classifier has to ask a
        # different question there.
        self.in_push_gate = False
        self.push_diff_base: Optional[str] = None
        self._passing_suite: Optional[PassingSuite] = None

    def run_command(self, command: List[str], check: bool = True) -> Tuple[int, str, str]:
        """Run a command and return exit code, stdout, stderr"""
        if self.debug:
            print(f"{Colors.CYAN}[DEBUG] Running: {' '.join(command)}{Colors.NC}")

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=check,
                cwd=self.project_root
            )
            if self.debug:
                print(f"{Colors.CYAN}[DEBUG] Exit code: {result.returncode}{Colors.NC}")
                if result.stdout:
                    print(f"{Colors.CYAN}[DEBUG] Stdout: {result.stdout[:200]}...{Colors.NC}" if len(result.stdout) > 200 else f"{Colors.CYAN}[DEBUG] Stdout: {result.stdout}{Colors.NC}")
                if result.stderr:
                    print(f"{Colors.CYAN}[DEBUG] Stderr: {result.stderr[:200]}...{Colors.NC}" if len(result.stderr) > 200 else f"{Colors.CYAN}[DEBUG] Stderr: {result.stderr}{Colors.NC}")
            return result.returncode, result.stdout, result.stderr
        except subprocess.CalledProcessError as e:
            if self.debug:
                print(f"{Colors.CYAN}[DEBUG] CalledProcessError: {e.returncode}{Colors.NC}")
            return e.returncode, e.stdout or "", e.stderr or ""
        except FileNotFoundError:
            if self.debug:
                print(f"{Colors.CYAN}[DEBUG] Command not found: {command[0]}{Colors.NC}")
            return 127, "", f"Command not found: {command[0]}"

    def check_git_status(self) -> CheckResult:
        """Check if working directory has unstaged changes"""
        code, stdout, _ = self.run_command(["git", "diff", "--name-only"])

        if code != 0:
            return CheckResult(
                name="Git Status",
                status=CheckStatus.FAILED,
                message="Failed to check git status"
            )

        # Check for unstaged changes
        unstaged = stdout.strip().split('\n') if stdout.strip() else []

        # Check for untracked files
        code, stdout, _ = self.run_command(["git", "ls-files", "--others", "--exclude-standard"])
        untracked = stdout.strip().split('\n') if stdout.strip() else []

        all_issues = []
        if unstaged:
            all_issues.extend([f"M {f}" for f in unstaged])
        if untracked:
            all_issues.extend([f"? {f}" for f in untracked])

        if all_issues:
            return CheckResult(
                name="Git Status",
                status=CheckStatus.FAILED,
                message="Unstaged or untracked changes detected",
                details=all_issues[:10]  # Show first 10 files
            )

        return CheckResult(
            name="Git Status",
            status=CheckStatus.PASSED,
            message="No unstaged changes"
        )

    def check_staged_files(self) -> CheckResult:
        """Check staged files are intentional - no git add -A"""
        # Get list of staged files
        code, stdout, _ = self.run_command(["git", "diff", "--cached", "--name-only"])

        if code != 0:
            return CheckResult(
                name="Staged Files Check",
                status=CheckStatus.FAILED,
                message="Failed to check staged files"
            )

        staged_files = stdout.strip().split('\n') if stdout.strip() else []

        # Check for suspicious patterns that indicate git add -A was used
        suspicious_patterns = [
            'container-home/',
            '.venv/',
            '__pycache__/',
            '.pytest_cache/',
            'node_modules/',
            '.git/',
        ]

        suspicious_staged = []
        for file in staged_files:
            for pattern in suspicious_patterns:
                if pattern in file:
                    suspicious_staged.append(file)

        if suspicious_staged:
            return CheckResult(
                name="Staged Files Check",
                status=CheckStatus.FAILED,
                message="Suspicious files staged (likely from git add -A)",
                details=suspicious_staged[:10]
            )

        # Warning if too many files staged at once
        if len(staged_files) > 20:
            return CheckResult(
                name="Staged Files Check",
                status=CheckStatus.WARNING,
                message=f"Large number of files staged ({len(staged_files)}) - verify intentional",
                details=staged_files[:10],
                is_critical=False
            )

        return CheckResult(
            name="Staged Files Check",
            status=CheckStatus.PASSED,
            message=f"{len(staged_files)} files staged"
        )

    def check_branch(self) -> CheckResult:
        """Check current branch"""
        code, stdout, _ = self.run_command(["git", "branch", "--show-current"])

        if code != 0:
            return CheckResult(
                name="Branch Check",
                status=CheckStatus.FAILED,
                message="Failed to check current branch"
            )

        branch = stdout.strip()
        if branch in ["main", "master"]:
            return CheckResult(
                name="Branch Check",
                status=CheckStatus.WARNING,
                message=f"On {branch} branch - be careful!",
                is_critical=False
            )

        return CheckResult(
            name="Branch Check",
            status=CheckStatus.PASSED,
            message=f"On feature branch: {branch}"
        )

    def _find_custom_test_runner(self) -> Optional[Path]:
        """Look for a project-supplied `gate-check-run-tests.sh` in the work repo.

        Projects can ship this script in their work repo info directories to
        override the default pytest invocation. Useful when the step must run
        inside a target container, with a non-trivial environment, or with a
        non-default test command.

        Discovery order (task-specific wins over global):
          1. {LMER_WORK_REPO_PATH}/{host}/{project}/{task}/info/gate-check-run-tests.sh
          2. {LMER_WORK_REPO_PATH}/{host}/{project}/info/gate-check-run-tests.sh

        The script must exist and be executable. Returns the resolved
        path, or None if no script is configured.
        """
        tsk_info_dir = task_info_dir()
        proj_info_dir = project_info_dir()
        if tsk_info_dir is None or proj_info_dir is None:
            return None

        # Discovery order: task-specific info wins over global project info.
        candidates = [
            tsk_info_dir / "gate-check-run-tests.sh",
            proj_info_dir / "gate-check-run-tests.sh",
        ]

        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _run_custom_test_runner(self, script: Path) -> CheckResult:
        """Run a project-supplied test runner script and map its exit code."""
        result = subprocess.run(
            [str(script)],
            capture_output=True,
            text=True,
            cwd=self.project_root,
        )

        combined_output = _capture(result)
        non_empty = [line for line in combined_output.splitlines() if line.strip()]

        # Goes on the message rather than into details, which print only under
        # --verbose and only as a 5-line tail — a runner declaring its mode up
        # front would fall off the end of that tail unread.
        #
        # Read per-stream, not off `combined_output`: the two streams are
        # captured separately, so concatenating them is not chronological —
        # every stderr line lands after every stdout line whichever was
        # written first. Scanning the concatenation would let a wrapper that
        # logs its own mode to stderr outrank the inner runner that printed
        # the marker on stdout and actually ran the suite. stdout wins, and
        # the last marker within a stream wins. Each stream is redacted first,
        # for the same reason `_capture` redacts: the label is printed, and a
        # runner that interpolated a token into its own mode line must not put
        # it on the gate's verdict.
        label = (runner_mode_label(redact_secrets(result.stdout or ""))
                 or runner_mode_label(redact_secrets(result.stderr or "")))
        mode_suffix = f" — {label}" if label else ""

        if result.returncode == 0:
            # Surface a tail of the runner's output so a verbose `gate-check`
            # run can confirm something actually executed (the pytest path
            # gets this for free via its `N tests passed` summary parsing).
            details = non_empty[-5:] if non_empty else None
            return CheckResult(
                name="Python Tests",
                status=CheckStatus.PASSED,
                message=f"Custom test runner passed ({script.name}){mode_suffix}",
                details=details,
                full_output=combined_output,
                # The terminal line is overwritten every run (/tmp/gate-check.log)
                # and never pushed; the receipt is the only durable record. Carry
                # the declared mode there too, so a reviewer reading the run dir a
                # week later can tell which suite the gate accepted. `scope_targets`
                # stays None — a project's runner owns its invocation, so this
                # gate still cannot say what it covered.
                scope=label,
            )

        details = non_empty[-5:] if non_empty else ["Custom test runner failed - no output"]
        return CheckResult(
            name="Python Tests",
            status=CheckStatus.FAILED,
            message=f"Custom test runner failed ({script.name}){mode_suffix}",
            details=details,
            full_output=combined_output,
            scope=label,
        )

    def _changed_paths(self) -> Optional[List[str]]:
        """Repo-relative paths this gate is about, or None for "cannot tell".

        Commit gate: the staged paths — but only when the index IS the change.
        Any unstaged or untracked file means there is a change this method has
        not looked at, and classifying a tree you have not seen is how a fast
        path turns into a waiver.

        Push gate: the paths in the commits being pushed, i.e. the diff from
        the remote-tracking ref to HEAD. A branch with no remote-tracking ref
        (first push) has no range to diff, so it answers None.

        None is never "nothing changed" — every caller treats it as "run
        everything".
        """
        # `-z`: NUL-separated and unquoted, so a non-ASCII path arrives as the
        # raw bytes git tracks rather than a C-quoted "caf\303\251.md" that
        # would match none of the text patterns (same reasoning as
        # check_secrets' `git ls-files -z`).
        #
        # `--no-renames`: with rename detection on, `--name-only` prints only
        # the POST-image, so `git mv src/foo.py docs/foo.md` reports one text
        # path and hides that a module was removed. The classifier has to see
        # both sides of a rename or it classifies half the change.
        if self.in_push_gate:
            if not self.push_diff_base:
                return None
            code, _, _ = self.run_command(
                ["git", "rev-parse", "--verify", "--quiet",
                 f"{self.push_diff_base}^{{commit}}"], check=False)
            if code != 0:
                return None
            code, stdout, _ = self.run_command(
                ["git", "diff", "--name-only", "--no-renames", "-z",
                 f"{self.push_diff_base}..HEAD"], check=False)
            if code != 0:
                return None
            return [p for p in stdout.split("\0") if p]

        for probe in (["git", "diff", "--name-only", "-z"],
                      ["git", "ls-files", "--others", "--exclude-standard", "-z"]):
            code, stdout, _ = self.run_command(probe, check=False)
            if code != 0 or [p for p in stdout.split("\0") if p]:
                return None

        code, stdout, _ = self.run_command(
            ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"],
            check=False)
        if code != 0:
            return None
        return [p for p in stdout.split("\0") if p]

    def _text_diff_subset(self) -> Tuple[List[str], Optional[Path]]:
        """The declared `tests.text_diff_subset` and the file declaring it.

        Undeclared is the default and means there is no fast path at all: a
        project that has not said which of its tests read text gets the full
        suite, always.
        """
        declared, source = self._gate_config_lookup("tests", "text_diff_subset")
        if not isinstance(declared, list):
            return [], None
        subset = [p.strip() for p in declared
                  if isinstance(p, str) and p.strip()]
        return (subset, source) if subset else ([], None)

    def _text_diff_fast_path(self) -> Optional[Tuple[List[str], List[str], Path]]:
        """(changed paths, subset, declaration file) when the tests check may
        run the declared text-reading subset instead of the whole tree.

        None means "run the full suite" — for an unclassifiable diff, a diff
        touching anything that is not prose, a project with no declaration,
        and for the kill switch.
        """
        if get_bool_env(TEXT_DIFF_FASTPATH_OFF_ENV):
            return None

        subset, source = self._text_diff_subset()
        if not subset or source is None:
            return None

        changed = self._changed_paths()
        if not changed:
            return None
        if not all(is_text_diff_path(path) for path in changed):
            return None

        missing = [p for p in subset if not (self.project_root / p).exists()]
        if missing:
            # A stale declaration would make pytest exit on a usage error and
            # report it as a test failure. Say so and run everything instead.
            print(f"{Colors.YELLOW}⚠️  Ignoring the text-diff subset declared "
                  f"in {source}: {', '.join(missing)} does not exist"
                  f"{Colors.NC}")
            return None

        return changed, subset, source

    def _print_text_diff_notice(self, changed: List[str], subset: List[str],
                                source: Path) -> None:
        """Announce a narrowed run: what changed, what will run, who said so.

        A fast path nobody can read is a waiver with better manners, so this
        names all three rather than summarizing.
        """
        try:
            declared_in = source.relative_to(self.project_root).as_posix()
        except ValueError:
            declared_in = str(source)
        print(f"{Colors.CYAN}⏭️  Text-only diff — {len(changed)} changed "
              f"path(s), all text:{Colors.NC}")
        for line in textwrap.wrap(", ".join(changed), width=68):
            print(f"    {line}")
        print(f"    Running the declared text-reading subset "
              f"({len(subset)} path(s)) instead of the full suite:")
        for line in textwrap.wrap(", ".join(subset), width=68):
            print(f"    {line}")
        print(f"    (declared in {declared_in} → tests.text_diff_subset)")

    def check_tests(self) -> CheckResult:
        """Run pytest tests, or a project-supplied custom test runner if present."""
        self._passing_suite = None
        custom_runner = self._find_custom_test_runner()
        if custom_runner is not None:
            return self._run_custom_test_runner(custom_runner)

        # Check if tests directory exists
        tests_dir = self.project_root / "tests"
        if not tests_dir.exists():
            return CheckResult(
                name="Python Tests",
                status=CheckStatus.PASSED,
                message="No tests directory found (skipped)"
            )

        # Determine python executable - prefer venv if it can actually import
        # pytest. In self-dev a bind-mounted host `.venv/bin/python` can resolve
        # to a system interpreter without the venv's site-packages, so existence
        # alone is insufficient — probe an import before trusting it.
        venv_python = self.project_root / ".venv" / "bin" / "python"
        if venv_python.exists() and self._interpreter_can_import(str(venv_python)):
            python_cmd = str(venv_python)
        else:
            python_cmd = "python"
            for cand in ("python3", "python"):
                if self._interpreter_can_import(cand):
                    python_cmd = cand
                    break

        # Prepend the dev src/ tree to PYTHONPATH so pytest imports the working
        # copy. Without this, an inherited PYTHONPATH (e.g. lmer self-dev's
        # `/Agents/global/src`) shadows the project's own source.
        env = os.environ.copy()
        src_dir = self.project_root / "src"
        if src_dir.is_dir():
            env["PYTHONPATH"] = (
                str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
            )

        # A text-only diff runs the declared text-reading subset instead of
        # the whole tree (#269) — nothing that could fail on the change is
        # skipped, the other ~8,600 tests simply are not asked.
        fast_path = self._text_diff_fast_path()
        if fast_path is None:
            targets = ["tests/"]
            scope = None
        else:
            changed, targets, source = fast_path
            self._print_text_diff_notice(changed, targets, source)
            scope = TEXT_DIFF_SCOPE

        pytest_argv = [python_cmd, "-m", "pytest", *targets, "-x",
                       "--tb=short", "-q",
                       "--ignore=tests/test_container_build.py"]

        # A tree already proven green in this environment is not proven again
        # (#269): the 0.7.0 release ran this suite five times over one
        # unchanged tree. The key covers the tree, everything uncommitted,
        # the argv above, the interpreter and what is installed around it, so
        # the narrowed run selected above composes a different key and can
        # never answer for a caller that needs the whole suite. The whole
        # environment the run below is handed is checked too — not just the
        # PYTHONPATH built above, because pytest reads PYTEST_ADDOPTS and this
        # suite's own tests read ambient state (tests/_lmer_runtime.py,
        # GIT_CONFIG_* in test_doctor_sources.py) — but out of the entry, so
        # a difference is a miss that can name itself. Unknown inputs mean no
        # key, which means the suite runs.
        cache_env = gate_cache.cache_environment(env)
        fingerprint = gate_cache.compute_fingerprint(
            self.run_command, pytest_argv, cache_env)
        cached = gate_cache.read_pass(fingerprint)
        if cached is not None:
            indexed = None
            if self.commit_handoff and gate_cache.cache_enabled():
                indexed = gate_cache.indexed_tree(self.run_command)
            if indexed is not None:
                self._passing_suite = PassingSuite(
                    argv=list(pytest_argv),
                    environment=dict(cache_env),
                    summary=cached.get("summary"),
                    indexed_tree=indexed,
                )
            return self._cached_tests_result(cached, fingerprint, scope,
                                             targets)
        cache_verdict, cache_reason = self._cache_miss_decision(fingerprint)
        self._print_cache_miss_notice(fingerprint)

        # Run pytest and capture output
        result = subprocess.run(
            pytest_argv,
            capture_output=True,
            text=True,
            cwd=self.project_root,
            env=env,
        )

        code = result.returncode
        stdout = result.stdout or ""
        combined_output = _capture(result)

        if code == 0:
            # Extract test count from pytest output
            test_count = "all tests"
            if "passed" in stdout:
                match = re.search(r'(\d+) passed', stdout)
                if match:
                    test_count = f"{match.group(1)} tests"

            if scope is not None:
                message = f"{scope}: {test_count} passed"
                details = [f"ran: {', '.join(targets)}"]
            else:
                message = f"All {test_count} passed"
                details = None

            self._passing_suite = self._record_passing_suite(
                fingerprint, pytest_argv, cache_env, combined_output
            )

            return CheckResult(
                name="Python Tests",
                status=CheckStatus.PASSED,
                message=message,
                details=details,
                full_output=combined_output,
                scope=scope,
                scope_targets=list(targets),
                cache_verdict=cache_verdict,
                cache_reason=cache_reason,
            )
        else:
            # Look for failure patterns in output
            failures = []

            # Check for FAILED or ERROR in output
            for line in combined_output.split('\n'):
                if 'FAILED' in line or 'ERROR' in line or 'ERRORS' in line:
                    failures.append(line.strip())
                # Also capture assertion errors
                elif 'AssertionError' in line or 'assert' in line.lower():
                    failures.append(line.strip())

            # If no specific failures found, but tests failed, show summary
            if not failures and 'failed' in combined_output.lower():
                # Try to extract summary line
                for line in combined_output.split('\n'):
                    if 'failed' in line.lower() and '=' not in line:
                        failures.append(line.strip())

            # If still no details, just indicate general failure
            if not failures:
                failures = ["Test suite failed - run pytest for details"]

            if scope is not None:
                failures = [f"ran: {', '.join(targets)}"] + failures

            return CheckResult(
                name="Python Tests",
                status=CheckStatus.FAILED,
                message="Tests failed" if scope is None
                        else f"Tests failed ({scope})",
                details=failures[:5],  # Show first 5 failures
                full_output=combined_output,
                scope=scope,
                scope_targets=list(targets),
                cache_verdict=cache_verdict,
                cache_reason=cache_reason,
            )

    def _cache_miss_decision(
        self, fingerprint: Optional[gate_cache.Fingerprint]
    ) -> Tuple[str, str]:
        """The structured verdict/reason recorded when no cache entry answered."""
        if not gate_cache.cache_enabled():
            return "disabled", f"disabled by {gate_cache.DISABLE_ENV}"
        if fingerprint is None:
            return "miss", "fingerprint unavailable"
        names = gate_cache.environment_mismatch(fingerprint)
        reason = gate_cache.environment_miss_reason(names)
        if reason:
            return "miss", reason
        return "miss", "no current matching pass"

    def _print_cache_miss_notice(
            self, fingerprint: Optional[gate_cache.Fingerprint]) -> None:
        """Say so when a recorded pass was missed on the environment alone (#269).

        The only miss worth a line: everything else about it is visible from
        the run that follows. This one is not — it is a suite re-run costing
        ten minutes because some variable moved, and without the line the
        answer to "why does the cache never hit?" is another measurement.
        Names only; the values are the environment's, and it holds tokens.
        """
        message = gate_cache.describe_miss(
            gate_cache.environment_mismatch(fingerprint))
        if message:
            print(f"{Colors.CYAN}ℹ️  {message}{Colors.NC}")

    def _cached_tests_result(self, entry: Dict[str, Any],
                             fingerprint: gate_cache.Fingerprint,
                             scope: Optional[str],
                             targets: List[str]) -> CheckResult:
        """The tests check's result when the cache answered instead (#269).

        Says so in every place a reader looks: on the terminal, in the
        `scope` field (so nothing has to parse a message to tell a reused
        pass from a fresh one), and in the captured output the gate-check log
        and `receipt_summary` read.
        """
        lines = gate_cache.describe_hit(entry, fingerprint)
        print(f"{Colors.GREEN}✅ Python Tests — cached result for this "
              f"exact tree{Colors.NC}")
        for line in lines:
            print(f"   {line}")

        summary = entry.get("summary")
        # Both halves always named: WHAT was reused is as load-bearing as the
        # fact that nothing ran, and a bare "cached" would leave a reader (or
        # a receipt) to assume the reused run was the whole suite.
        cached_scope = f"{CACHE_SCOPE} {scope or FULL_SUITE_SCOPE}"
        # The cached suite summary goes LAST: receipt_summary reads the final
        # countable line, and a receipt saying "cached full suite: 8727
        # passed, 40 skipped" is the answer to "was this actually tested?".
        captured = ["The suite did NOT run: a passing result for this exact "
                    "tree was reused."]
        captured.extend(lines)
        captured.append("Cached invocation: " + " ".join(entry.get("argv") or []))
        if summary:
            captured.append(summary)

        return CheckResult(
            name="Python Tests",
            status=CheckStatus.PASSED,
            message=f"{cached_scope}: {summary}" if summary
                    else f"{cached_scope}: earlier passing run reused",
            full_output="\n".join(captured),
            scope=cached_scope,
            scope_targets=list(targets),
            cache_verdict="hit",
            cache_reason="exact fingerprint pass found",
        )

    def _record_passing_suite(self,
                              fingerprint: Optional[gate_cache.Fingerprint],
                              pytest_argv: List[str],
                              cache_env: Dict[str, str], output: str
                              ) -> Optional[PassingSuite]:
        """Record a passing suite for the tree it actually ran on (#269).

        The fingerprint is re-taken and must still match, because a suite
        takes ~10 minutes and the tree can move inside that window. What this
        catches is an edit that is STILL THERE when the suite ends: the two
        fingerprints are taken before and after, so an edit made at minute 8
        and reverted at minute 10 leaves them equal and is not detected. That
        residual window is accepted rather than closed — closing it would mean
        watching the tree for the whole run — and the check costs two git
        commands against a run that just cost minutes.
        """
        if fingerprint is None:
            return None
        confirmed = gate_cache.compute_fingerprint(
            self.run_command, pytest_argv, cache_env)
        if confirmed is None or confirmed.key != fingerprint.key:
            return None
        summary = pytest_summary_line(output)
        recorded = gate_cache.record_pass(
            confirmed,
            summary=summary,
            argv=pytest_argv,
            gate=receipt_argv()[0],
        )
        if recorded is None:
            return None
        if not self.commit_handoff:
            return None
        indexed = gate_cache.indexed_tree(self.run_command)
        if indexed is None:
            return None
        return PassingSuite(
            argv=list(pytest_argv),
            environment=dict(cache_env),
            summary=summary,
            indexed_tree=indexed,
        )

    def handoff_test_cache_after_commit(self) -> bool:
        """File the passing suite under the equivalent clean post-commit key.

        The pre-commit key is the old HEAD plus staged state; the post-commit key
        is the new HEAD plus a clean tree. Handoff is allowed only when the new
        committed tree equals the index tree captured with the passing suite and
        the recomputed fingerprint is clean. Any uncertainty declines reuse.
        """
        try:
            suite = self._passing_suite
            if suite is None:
                return False
            committed = gate_cache.committed_tree(self.run_command)
            if committed is None or committed != suite.indexed_tree:
                return False
            post_commit = gate_cache.compute_fingerprint(
                self.run_command, suite.argv, suite.environment
            )
            if (
                post_commit is None
                or not post_commit.clean
                or post_commit.tree != suite.indexed_tree
            ):
                return False
            return gate_cache.record_pass(
                post_commit,
                summary=suite.summary,
                argv=suite.argv,
                gate="gate-commit",
            ) is not None
        except Exception:
            # The cache is an optimization. No handoff failure may change the
            # result of the commit that already succeeded.
            return False

    @staticmethod
    def _interpreter_can_import(python_cmd: str, module: str = "pytest") -> bool:
        """True iff ``python_cmd -c 'import <module>'`` succeeds.

        A bind-mounted host venv can leave a ``.venv/bin/python`` that resolves to
        a system interpreter without the venv's site-packages (self-dev), so a mere
        existence check is insufficient — probe an actual import.
        """
        try:
            return subprocess.run(
                [python_cmd, "-c", f"import {module}"],
                capture_output=True, timeout=30,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _venv_script_launchable(script_path: Path) -> bool:
        """True iff `script_path` exists AND its shebang interpreter is reachable.

        A bind-mounted host venv can leave a script on disk whose shebang points
        at an interpreter path that does not exist in this filesystem (e.g., a
        host-side `.venv` mounted into a container). `subprocess.run` then fails
        with ENOENT naming the *script*, even though the missing file is the
        interpreter — so a plain `path.exists()` check is not sufficient.
        """
        if not script_path.exists():
            return False
        try:
            with open(script_path, "rb") as f:
                first_line = f.readline()
        except OSError:
            return False
        if not first_line.startswith(b"#!"):
            return True
        tokens = first_line[2:].strip().split()
        if not tokens:
            return True
        try:
            interpreter = Path(tokens[0].decode("utf-8", errors="strict"))
        except UnicodeDecodeError:
            return False
        return interpreter.exists()

    def _resolve_precommit_command(self) -> List[str]:
        """Pick the best pre-commit invocation for this project.

        Order: project venv binary (`.venv/bin/pre-commit`) if it exists and
        its shebang interpreter is launchable, then a bare `pre-commit` from
        PATH.

        Earlier versions fell back to `uv run pre-commit` / `poetry run pre-commit`
        when the project had `uv.lock` / `poetry.lock`. Both `uv run` and
        `poetry run` sync the project's full dependency set before executing
        the command — for any project whose runtime deps require system
        libraries the gate environment can't build (e.g. `mysqlclient` without
        MySQL dev headers), the sync fails before pre-commit is ever invoked,
        and gate-check reports a "Pre-commit Hooks" failure that has nothing
        to do with hooks. Bare `pre-commit` doesn't trigger any project sync
        (hooks manage their own isolated envs from `.pre-commit-config.yaml`'s
        pins), so it's the safer default.
        """
        venv_precommit = self.project_root / ".venv" / "bin" / "pre-commit"
        if self._venv_script_launchable(venv_precommit):
            return [str(venv_precommit)]

        return ["pre-commit"]

    def check_precommit(self) -> CheckResult:
        """Run pre-commit hooks, optionally reusing an exact recent full pass."""
        precommit_cmd = self._resolve_precommit_command()
        argv = precommit_cmd + ["run", "--all-files"]
        reuse, _source = self._gate_config_lookup(
            "precommit", "reuse_all_files", repo_local=False
        )
        fingerprint = None
        if reuse is True:
            fingerprint = precommit_cache.compute_fingerprint(
                self.run_command,
                self.project_root,
                precommit_cmd,
                argv,
                os.environ,
            )
            cached = precommit_cache.read_pass(fingerprint)
            if cached is not None:
                age = max(0, int(time.time() - float(cached["created_at"])))
                return CheckResult(
                    name="Pre-commit Hooks",
                    status=CheckStatus.PASSED,
                    message="Reused recent full --all-files pass",
                    details=[
                        f"Exact checked content, Git hook state, config, hooks, "
                        f"executable, argv and environment passed {age}s ago."
                    ],
                    full_output="Pre-commit cache hit: exact full --all-files pass\n",
                )

        # Run pre-commit and capture output
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=self.project_root
        )

        code = result.returncode
        combined_output = _capture(result)

        if code == 0:
            if fingerprint is not None:
                after = precommit_cache.compute_fingerprint(
                    self.run_command,
                    self.project_root,
                    precommit_cmd,
                    argv,
                    os.environ,
                )
                # A hook that changed any checked content (including an
                # autofix) did not prove the original fingerprint green.
                if after == fingerprint:
                    precommit_cache.record_pass(fingerprint)
            return CheckResult(
                name="Pre-commit Hooks",
                status=CheckStatus.PASSED,
                message="All hooks passed",
                full_output=combined_output,
            )

        # On failure: surface the tail of combined stdout+stderr. Earlier
        # versions parsed for lines containing "Failed", which silently
        # discarded everything else — including messages from things that
        # *impersonate* pre-commit (e.g. `uv run pre-commit` printing a uv
        # build error before pre-commit ever runs). Showing the raw tail
        # makes the real failure visible instead of inventing a summary.
        combined = combined_output.rstrip()
        if combined:
            lines = [line for line in combined.split('\n') if line.strip()]
            details = lines[-15:]
        else:
            details = [f"pre-commit exited {code} with no output"]

        return CheckResult(
            name="Pre-commit Hooks",
            status=CheckStatus.FAILED,
            message=f"pre-commit exited {code}",
            details=details,
            full_output=combined_output,
        )

    def _gate_config_sources(self, repo_local: bool = True
                             ) -> List[Tuple[Path, Dict[str, Any]]]:
        """Every readable gate-check config, in precedence order.

        1. `{project_root}/.lmer/gate-check.yaml` — the target repo's own
           declaration. Settings that name that repo's files (the text-diff
           test subset) belong beside them and version with them, and this is
           the only location a CI runner is guaranteed to have.
        2. `{LMER_WORK_REPO_PATH}/{host}/{project}/info/gate-check.yaml` — the
           work repo's project info, where operator-owned settings that may
           silence work live (the secrets ignore list and pre-commit reuse).

        `repo_local=False` drops source 1, for settings that must NOT be
        readable from the gated repo: source 1 is inside the tree the agent
        is editing, so anything read from it is a setting the gated code can
        rewrite about its own gating. It is right for the test subset (naming
        a repo's own tests is a claim the guard test re-derives) and wrong for
        `secrets.ignore` or pre-commit reuse, which silence a check.

        A missing file, unreadable file or unparseable YAML contributes
        nothing; this never raises. Lookup is per key (_gate_config_lookup),
        so a repo-local file that declares only `tests` does not hide a
        `secrets` block in the work repo.
        """
        search_dirs = [self.project_root / ".lmer"] if repo_local else []
        info_dir = project_info_dir()
        if info_dir is not None:
            search_dirs.append(info_dir)

        sources: List[Tuple[Path, Dict[str, Any]]] = []
        for directory in search_dirs:
            for candidate in ("gate-check.yaml", "gate-check.yml"):
                config_path = directory / candidate
                if not config_path.is_file():
                    continue
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = yaml.safe_load(f)
                except (IOError, OSError, yaml.YAMLError):
                    break
                if isinstance(config, dict):
                    sources.append((config_path, config))
                break
        return sources

    def _gate_config_lookup(self, *keys: str, repo_local: bool = True
                            ) -> Tuple[Any, Optional[Path]]:
        """First value found at `keys` across the config sources, and its file.

        `repo_local` is passed through to _gate_config_sources — the caller
        decides whether the gated repo may answer for this key.

        Returns (None, None) when no source carries the key path.
        """
        for config_path, config in self._gate_config_sources(repo_local):
            value: Any = config
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    value = None
                    break
                value = value[key]
            if value is not None:
                return value, config_path
        return None, None

    def _load_secrets_ignore_patterns(self) -> List[str]:
        """Load secrets-check ignore globs from the work repo's project info.

        Work-repo-only, deliberately: this list is what silences
        `check_secrets`, so honoring a copy inside the gated repo would let
        the repo being scanned (or an agent editing it) add
        `secrets: {ignore: ["**/*"]}` to `.lmer/gate-check.yaml` and turn the
        check off. The work repo is outside the tree under review, which is
        the property the allowlist rests on.

        Returns the `secrets.ignore` list, or an empty list when no config
        declares one — the setting is optional. A non-list value (a bare
        string) is ignored rather than iterated: `for p in "*"` yields the
        pattern `*`, which would match every path and silence the scan.
        """
        ignore, _ = self._gate_config_lookup("secrets", "ignore",
                                             repo_local=False)
        if not isinstance(ignore, list):
            return []
        return [str(p) for p in ignore if isinstance(p, str)]

    def check_secrets(self) -> CheckResult:
        """Check for hardcoded secrets"""
        patterns = [
            (r'(api_key|API_KEY|secret|SECRET|password|PASSWORD|token|TOKEN)\s*=\s*["\'][^"\']+["\']',
             "Potential hardcoded secret"),
            (r'AKIA[0-9A-Z]{16}', "AWS Access Key"),
            (r'BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY', "Private Key"),
        ]

        exclude_dirs = ['.git', '.venv', '__pycache__', 'node_modules', 'container-home']
        exclude_files = ['gates.py', 'test_*.py', 'pc.py', 'test_gates.py']

        ignore_patterns = self._load_secrets_ignore_patterns()

        # Only scan files tracked by git. Untracked and git-ignored files will
        # never be committed, so scanning them for to-be-committed secrets just
        # produces false positives (e.g. vendored, git-ignored dependencies).
        # If this is not a git repo, fall back to scanning everything.
        #
        # Use `-z` (NUL-separated, no quoting) so paths with non-ASCII bytes,
        # special characters, or embedded newlines match the raw paths produced
        # by os.walk below. Plain `git ls-files` would quote such paths (e.g.
        # "caf\303\251.py") and split on newlines, causing those tracked files
        # to be treated as untracked and silently skipped by the scan.
        tracked_files = None
        code, stdout, _ = self.run_command(["git", "ls-files", "-z"], check=False)
        if code == 0:
            tracked_files = set(p for p in stdout.split("\0") if p)

        findings = []

        for root, dirs, files in os.walk(self.project_root):
            # Remove excluded directories
            dirs[:] = [d for d in dirs if d not in exclude_dirs]

            for file in files:
                # Check if file should be excluded
                if any(file.startswith(ex.replace('*', '')) if '*' in ex else file == ex for ex in exclude_files):
                    continue
                # Also skip all test files
                if file.startswith('test_') and file.endswith('.py'):
                    continue

                if not file.endswith(('.py', '.sh', '.yml', '.yaml', '.json', '.env')):
                    continue

                filepath = Path(root) / file
                relative_path = filepath.relative_to(self.project_root)
                relative_str = relative_path.as_posix()

                if tracked_files is not None and relative_str not in tracked_files:
                    continue

                if any(fnmatch.fnmatch(relative_str, pat) for pat in ignore_patterns):
                    continue

                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        for pattern, desc in patterns:
                            if re.search(pattern, content):
                                findings.append(f"{relative_path}: {desc}")
                except Exception:
                    continue

        if findings:
            return CheckResult(
                name="Security Check",
                status=CheckStatus.FAILED,
                message="Potential secrets detected",
                details=findings[:5]
            )

        return CheckResult(
            name="Security Check",
            status=CheckStatus.PASSED,
            message="No secrets detected"
        )

    def check_code_quality(self) -> CheckResult:
        """Check code quality issues"""
        issues = []

        # Check for print statements
        for py_file in self.project_root.rglob("*.py"):
            # Exclude directories and files where print statements are expected
            if any(exclude in str(py_file) for exclude in ['.venv', 'container-home', 'bin/', 'hooks/', 'Ctl/']):
                continue
            # Exclude test files and gate system itself
            if 'test_' in py_file.name or py_file.name == 'gates.py':
                continue

            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    for i, line in enumerate(f, 1):
                        if 'print(' in line and not line.strip().startswith('#'):
                            relative = py_file.relative_to(self.project_root)
                            issues.append(f"{relative}:{i} - print statement")
                        if 'pdb.set_trace' in line or 'breakpoint()' in line:
                            relative = py_file.relative_to(self.project_root)
                            issues.append(f"{relative}:{i} - debug code")
            except Exception:
                continue

        if issues:
            return CheckResult(
                name="Code Quality",
                status=CheckStatus.WARNING,
                message="Quality issues found",
                details=issues[:5],
                is_critical=False
            )

        return CheckResult(
            name="Code Quality",
            status=CheckStatus.PASSED,
            message="No quality issues"
        )

    def _read_provisioned_docs(self) -> List[str]:
        """Read the list of lmer-provisioned documentation files."""
        marker = self.project_root / ".lmer-provisioned-docs"
        if not marker.exists():
            return []
        return [line.strip() for line in marker.read_text().splitlines() if line.strip()]

    def check_documentation(self) -> CheckResult:
        """Check documentation exists"""
        required_docs = {
            'README.md': True,
            'AGENTS.md': True,
            'rules/git.md': True,
            'rules/testing.md': True,
        }

        missing = []
        for doc, critical in required_docs.items():
            if not (self.project_root / doc).exists():
                missing.append(doc)

        if missing:
            critical_missing = [d for d in missing if required_docs.get(d, False)]
            if critical_missing:
                return CheckResult(
                    name="Documentation",
                    status=CheckStatus.FAILED,
                    message="Critical documentation missing",
                    details=critical_missing
                )
            else:
                return CheckResult(
                    name="Documentation",
                    status=CheckStatus.WARNING,
                    message="Some documentation missing",
                    details=missing,
                    is_critical=False
                )

        # Check if any required docs were provisioned by lmer (not from the project itself)
        provisioned = self._read_provisioned_docs()
        provisioned_required = [d for d in required_docs if d in provisioned]
        if provisioned_required:
            return CheckResult(
                name="Documentation",
                status=CheckStatus.WARNING,
                message="Using lmer default documentation (project repo does not provide its own)",
                details=[
                    f"Provisioned by lmer: {', '.join(provisioned_required)}",
                    "Consider adding project-specific AGENTS.md and rules/ to the repository",
                ],
                is_critical=False
            )

        return CheckResult(
            name="Documentation",
            status=CheckStatus.PASSED,
            message="All documentation present"
        )

    def _staged_changelog_fragments(self, ctl_fragment_mode: bool) -> List[str]:
        """Return staged changelog.d/ fragment files.

        Only files directly under changelog.d/ count; dotfiles and README
        are never fragments. In ctl mode only *.yaml/*.yml files count; in
        other-tool mode (towncrier/scriv) any extension counts.
        --diff-filter=ACMR: a staged DELETION of a fragment (e.g. a revert,
        or a release commit removing rolled fragments) is not itself a
        changelog update.
        """
        code, stdout, _ = self.run_command(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
        )
        if code != 0:
            return []
        staged_files = stdout.strip().split('\n') if stdout.strip() else []
        return [
            f for f in staged_files
            if Path(f).parent == Path("changelog.d")
            and not Path(f).name.startswith(".")
            and Path(f).stem.upper() != "README"
            and (
                not ctl_fragment_mode
                or Path(f).suffix.lower() in (".yaml", ".yml")
            )
        ]

    def check_changelog(self) -> CheckResult:
        """Check if a changelog file exists and has been updated in staged changes."""
        # Common changelog file patterns (case-insensitive matching via .upper())
        changelog_patterns = [
            "CHANGELOG", "CHANGES", "HISTORY", "NEWS", "RELEASES",
        ]

        # Find existing changelog files in project root
        existing_changelogs = []
        for item in self.project_root.iterdir():
            if not item.is_file():
                continue
            stem = item.stem
            for pattern in changelog_patterns:
                if stem.upper() == pattern.upper():
                    existing_changelogs.append(item.name)
                    break

        # changelog.d fragment support: a staged fragment counts as a
        # changelog update. Mode follows the changelog file: with a YAML
        # changelog (or none at all — a fragment-only project pre first
        # release) the repo is ctl-style and only *.yaml/*.yml fragments
        # count; with a non-YAML changelog (CHANGELOG.md etc.) the
        # changelog.d/ convention belongs to another tool (towncrier/scriv)
        # and any staged fragment file counts, whatever its extension.
        # Checked before the "no changelog file" early return so
        # fragment-only projects still pass. Known limitation: only the
        # default layout (a root-level changelog.d/ beside the changelog) is
        # implemented; ctl's configurable fragments_dir is not consulted.
        changelog_d = self.project_root / "changelog.d"
        ctl_fragment_mode = not existing_changelogs or any(
            Path(f).suffix.lower() in (".yaml", ".yml") for f in existing_changelogs
        )
        if changelog_d.is_dir():
            staged_fragments = self._staged_changelog_fragments(ctl_fragment_mode)
            if staged_fragments:
                return CheckResult(
                    name="Changelog",
                    status=CheckStatus.PASSED,
                    message=f"Changelog fragment staged: {', '.join(staged_fragments)}"
                )

        if not existing_changelogs:
            details = ["Consider adding a changelog to communicate changes to users"]
            if changelog_d.is_dir():
                # Fragment-only repo (changelog file materializes at first
                # release): point at the repo's own convention. No changelog
                # file means ctl_fragment_mode is True by construction, so
                # the ctl-style hint is always the right one here.
                details.append(CTL_FRAGMENT_HINT)
            return CheckResult(
                name="Changelog",
                status=CheckStatus.WARNING,
                message="No changelog file found (e.g., CHANGELOG.yaml, CHANGES.md)",
                details=details,
                is_critical=False
            )

        # Check if any changelog file is in the staged changes
        code, stdout, _ = self.run_command(["git", "diff", "--cached", "--name-only"])
        if code != 0:
            return CheckResult(
                name="Changelog",
                status=CheckStatus.WARNING,
                message="Could not check staged files for changelog updates",
                is_critical=False
            )

        staged_files = stdout.strip().split('\n') if stdout.strip() else []
        staged_changelogs = [f for f in staged_files if f in existing_changelogs]

        if not staged_changelogs:
            details = ["Update the changelog if this commit includes user-facing changes"]
            if changelog_d.is_dir():
                details.append(
                    CTL_FRAGMENT_HINT if ctl_fragment_mode else OTHER_TOOL_FRAGMENT_HINT
                )
            return CheckResult(
                name="Changelog",
                status=CheckStatus.WARNING,
                message=f"Changelog not updated (found: {', '.join(existing_changelogs)})",
                details=details,
                is_critical=False
            )

        return CheckResult(
            name="Changelog",
            status=CheckStatus.PASSED,
            message=f"Changelog updated: {', '.join(staged_changelogs)}"
        )

    def check_deliverable_formats(self) -> CheckResult:
        """Warn when staged spec-class deliverables use binary document formats.

        Specs, plans, and reports are Markdown deliverables — a .docx spec is
        unreviewable in GitLab (undiffable, unlinkable at line level) (#102).
        WARNING, not a hard fail: reports from external sources may
        legitimately arrive as e.g. PDF.
        """
        code, stdout, _ = self.run_command(["git", "diff", "--cached", "--name-only"])
        if code != 0:
            return CheckResult(
                name="Deliverable Format",
                status=CheckStatus.WARNING,
                message="Could not check staged files for deliverable formats",
                is_critical=False
            )

        staged_files = stdout.strip().split('\n') if stdout.strip() else []

        offenders = []
        for file in staged_files:
            path = Path(file)
            if path.suffix.lower() not in BINARY_DOC_EXTENSIONS:
                continue
            # Only paths that name a spec-class deliverable in any component
            # (spec.docx, docs/specs/api.pdf, project-plan.odt). Other binary
            # documents — vendored manuals, fixtures — are out of scope.
            if any(DELIVERABLE_NAME_RE.search(part) for part in path.parts):
                offenders.append(file)

        if offenders:
            return CheckResult(
                name="Deliverable Format",
                status=CheckStatus.WARNING,
                message="Spec-class deliverable staged in a binary document format",
                details=[
                    f"{f}: specs, plans, and reports deliver as Markdown (.md) "
                    "— never docx/pdf/binary documents"
                    for f in offenders[:5]
                ],
                is_critical=False
            )

        return CheckResult(
            name="Deliverable Format",
            status=CheckStatus.PASSED,
            message="No binary-document deliverables staged"
        )

    def check_permissions(self) -> CheckResult:
        """Check file permissions"""
        issues = []

        # Check for world-writable files
        for item in self.project_root.rglob("*"):
            if '.git' in str(item) or '.venv' in str(item):
                continue

            if item.is_file():
                stat = item.stat()
                if stat.st_mode & 0o002:  # World writable
                    relative = item.relative_to(self.project_root)
                    issues.append(f"{relative}: world-writable")

        # Check scripts are executable
        for script_dir in ['bin', 'hooks']:
            script_path = self.project_root / script_dir
            if script_path.exists():
                for script in script_path.iterdir():
                    if script.is_file() and (script.suffix in ['.sh', ''] or 'gate' in script.name):
                        if not os.access(script, os.X_OK):
                            relative = script.relative_to(self.project_root)
                            issues.append(f"{relative}: not executable")

        if issues:
            return CheckResult(
                name="File Permissions",
                status=CheckStatus.FAILED,
                message="Permission issues found",
                details=issues[:5]
            )

        return CheckResult(
            name="File Permissions",
            status=CheckStatus.PASSED,
            message="Permissions correct"
        )

    def _tally(self) -> Tuple[int, int]:
        """Count critical failures and warnings across all check results.

        Shared by print_results and write_log_file so the two summary counts
        can't drift apart. Non-critical failures are not counted as failures.
        """
        failures = 0
        warnings = 0
        for result in self.results:
            if result.status == CheckStatus.FAILED and result.is_critical:
                failures += 1
            elif result.status == CheckStatus.WARNING:
                warnings += 1
        return failures, warnings

    def receipt_summary(self) -> Optional[str]:
        """Best-effort one-line summary of this run for gate receipts (#88).

        On a failing run: the names of the critically failed checks. On a
        passing run: the test runner's own tail line (the pytest summary)
        when one is parseable from the tests check's captured output, tagged
        with the run's scope when it was narrowed (#269) so a reader can tell
        a subset run from a full one — and when the suite did not run at all
        because the cache answered for it ("cached full suite: 8727 passed,
        …"), since the receipt is what a reviewer reads later when asking
        whether this was actually tested. A project runner's declared mode
        (#307) rides the same prefix ("root uv suite (no service container):
        198 passed, …"), so a fallback suite cannot read back as the
        project's usual one. None when neither exists — the
        receipt's `summary` field is then simply absent, never fabricated.

        Prose, and best-effort: `receipt_test_fields` is what a machine
        reader should use to tell the three run shapes apart.
        """
        failed = [
            result.name for result in self.results
            if result.status == CheckStatus.FAILED and result.is_critical
        ]
        if failed:
            return "failed: " + ", ".join(failed)
        for result in self.results:
            if result.name == "Python Tests" and result.full_output:
                line = pytest_summary_line(result.full_output)
                if line is None:
                    continue
                if result.scope:
                    return f"{result.scope}: {line}"
                return line
        return None

    def receipt_test_fields(self) -> Dict[str, Any]:
        """The receipt's structured test-coverage kwargs for this run (#269).

        Shared by the gate bins for the same reason as `receipt_argv`: three
        callers, one definition of what the fields mean. `emit_gate_event`
        records `outcome` 'pass' with exit code 0 whether the whole suite
        ran, the declared text-diff subset ran, or nothing ran because an
        earlier pass on this exact tree was reused — so the scope has to
        reach the receipt as data, not as a prefix on the free-text summary.

        Returns no keys at all when this run cannot say what the tests check
        covered: a project's own runner owns its invocation, a missing tests
        directory ran nothing, and a gate that skipped tests has no tests
        result. Absent is "unknown", and unknown must never read as "full".
        """
        for result in self.results:
            if result.name != "Python Tests" or result.scope_targets is None:
                continue
            return {
                # `scope` is None for a fresh full run (nothing to disclaim on
                # the terminal); the receipt names it instead of implying it.
                "test_scope": result.scope or FULL_SUITE_SCOPE,
                "test_targets": list(result.scope_targets),
                "test_cache_verdict": result.cache_verdict or "unknown",
                "test_cache_reason": (
                    result.cache_reason or "cache decision unavailable"
                ),
            }
        return {}

    def print_results(self):
        """Print all check results"""
        print(f"\n{Colors.BLUE}{'═' * 60}{Colors.NC}")
        print(f"{Colors.BLUE}{Colors.BOLD}                    GATE CHECK RESULTS{Colors.NC}")
        print(f"{Colors.BLUE}{'═' * 60}{Colors.NC}\n")

        failures, warnings = self._tally()

        for result in self.results:
            # Choose color based on status
            if result.status == CheckStatus.PASSED:
                color = Colors.GREEN
            elif result.status == CheckStatus.FAILED:
                color = Colors.RED
            elif result.status == CheckStatus.WARNING:
                color = Colors.YELLOW
            else:
                color = Colors.CYAN

            # Print main result
            print(f"{color}{result.status.value}{Colors.NC} {result.name}")
            if result.message:
                print(f"    {result.message}")

            # Print details if any
            if result.details and self.verbose:
                for detail in result.details:
                    print(f"      • {detail}")

        # Summary
        print(f"\n{Colors.BLUE}{'═' * 60}{Colors.NC}")
        print(f"{Colors.BLUE}{Colors.BOLD}                       SUMMARY{Colors.NC}")
        print(f"{Colors.BLUE}{'═' * 60}{Colors.NC}\n")

        if failures == 0:
            if warnings == 0:
                print(f"{Colors.GREEN}{Colors.BOLD}✅ ALL CHECKS PASSED!{Colors.NC}")
            else:
                print(f"{Colors.GREEN}✅ All critical checks passed{Colors.NC}")
                print(f"{Colors.YELLOW}⚠️  {warnings} warning(s) found{Colors.NC}")
            return True
        else:
            print(f"{Colors.RED}{Colors.BOLD}❌ GATE BLOCKED - {failures} critical failure(s){Colors.NC}")
            if warnings > 0:
                print(f"{Colors.YELLOW}⚠️  Also found {warnings} warning(s){Colors.NC}")
            return False

    def write_log_file(self, path: Optional[Path] = None) -> Optional[Path]:
        """Persist the full results of this run to a plain-text log file.

        The terminal output only shows a short tail of each check's details
        (and only when --verbose). This writes every check's status, message,
        all details, and the complete captured subprocess output (for tests
        and pre-commit) so a failure can be investigated by reading the file
        instead of re-running the slow checks.

        Returns the path written, or None if writing failed (writing the log
        must never break gate-check itself).
        """
        # Resolve the default at call time (not as a default arg) so the module
        # constant can be monkeypatched and so the latest value is always used.
        if path is None:
            path = GATE_CHECK_LOG_PATH

        lines: List[str] = []
        sep = "=" * 78
        lines.append(sep)
        lines.append("GATE CHECK LOG")
        lines.append(f"Generated:         {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.timestamp))}")
        lines.append(f"Working directory: {self.project_root}")
        lines.append(sep)
        lines.append("")

        failures, warnings = self._tally()
        for result in self.results:
            # Strip the emoji/whitespace decoration from the status value so the
            # log greps cleanly (e.g. "PASSED", "FAILED").
            status_label = result.status.name
            lines.append(f"[{status_label}] {result.name}")
            if result.message:
                lines.append(f"  Message: {result.message}")
            if not result.is_critical and result.status == CheckStatus.FAILED:
                lines.append("  (non-critical)")
            if result.details:
                lines.append("  Details:")
                for detail in result.details:
                    lines.append(f"    - {detail}")
            if result.full_output is not None:
                trimmed = result.full_output.strip("\n")
                lines.append("  Full output:")
                lines.append("  " + ("-" * 76))
                if trimmed:
                    lines.extend(trimmed.splitlines())
                else:
                    lines.append("(no output)")
                lines.append("  " + ("-" * 76))
            lines.append("")

        lines.append(sep)
        lines.append(f"SUMMARY: {failures} critical failure(s), {warnings} warning(s)")
        lines.append(sep)
        lines.append("")

        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as e:
            print(f"{Colors.YELLOW}⚠️  Could not write gate-check log to {path}: {e}{Colors.NC}")
            return None
        return path

    def _critical_failures_so_far(self) -> List[str]:
        """Names of the critical checks that have already failed this run.

        The fail-fast trigger for the test suite (#269): once one of these
        exists the gate is already blocked, so the suite's verdict cannot
        change the outcome — only how long the developer waits to hear it.
        """
        return [
            result.name for result in self.results
            if result.status == CheckStatus.FAILED and result.is_critical
        ]

    def run_commit_gate(self, skip_tests: bool = False) -> bool:
        """Run all checks for commit gate.

        When skip_tests=True, the long-running test suite is skipped. Callers
        are responsible for any caller-specific user-facing rationale (e.g.
        gate-commit prints a QUICK_GATE_COMMIT hint before invoking).
        """
        # Cheapest-first, with the suite always last (#269). The suite is
        # ~10 minutes and every other check is seconds, so this ordering plus
        # the fail-fast below is what keeps a formatting failure from costing
        # a full suite run. Only the suite is short-circuited: every cheap
        # check still runs to completion, so one pass surfaces every cheap
        # problem rather than one problem per pass.
        checks = [
            self.check_git_status,
            self.check_staged_files,  # Check for git add -A abuse
            self.check_branch,
            self.check_secrets,
            self.check_permissions,
            self.check_deliverable_formats,
            self.check_changelog,
            self.check_documentation,
            self.check_code_quality,
            self.check_precommit,
            self.check_tests,
        ]

        if skip_tests:
            print(f"{Colors.YELLOW}⚠️  Skipping Python Tests{Colors.NC}")
            # remove() compares by __eq__ — bound methods compare equal, and
            # this fails loudly with ValueError if check_tests is ever renamed
            # or removed, rather than silently making skip_tests a no-op.
            checks.remove(self.check_tests)

        for check in checks:
            if check == self.check_tests:
                blockers = self._critical_failures_so_far()
                if blockers:
                    print(f"{Colors.YELLOW}⏭️  Skipping Python Tests — "
                          f"{len(blockers)} earlier check(s) failed "
                          f"({', '.join(blockers)}){Colors.NC}")
                    print("    Fix those first; the suite has not been run.")
                    # Recorded as a result, not merely printed: the terminal
                    # scrolls, but print_results, the gate-check log and the
                    # receipt all read this list — a reader has to be able to
                    # tell "suite green" from "suite never ran".
                    self.results.append(CheckResult(
                        name="Python Tests",
                        status=CheckStatus.SKIPPED,
                        message="Not run — earlier failures: "
                                + ", ".join(blockers),
                    ))
                    continue

            print(f"Running {check.__name__.replace('check_', '').replace('_', ' ').title()}...")
            result = check()
            self.results.append(result)

        passed = self.print_results()

        # Always persist the full results so failures can be investigated by
        # reading the log instead of re-running the (often slow) checks.
        log_path = self.write_log_file()
        if log_path is not None:
            print(f"\n{Colors.CYAN}📝 Full check log written to: {log_path}{Colors.NC}")

        return passed

    def _parse_push_allow_entry(self, entry: str) -> Optional[Tuple[str, str]]:
        """Parse one allow-list entry into (repo, ref_pattern).

        An entry is either a bare repo spec or ``repo|refpattern``:

        - ``repo`` is matched by the :mod:`lmer_cli.push_allow` grammar —
          exact repo, whole host, ``*.domain`` wildcard, host + project
          prefix, or a legacy host-less project path (#107). It replaced
          the original unanchored substring rule, which also matched a
          host that merely EMBEDDED the allowed path. That module's
          docstring is the authoritative statement of the repo half.
        - ``refpattern`` is an fnmatch pattern tested against the
          fully-qualified target ref, e.g. ``refs/tags/*`` or
          ``refs/heads/main``.
        - The delimiter is ``|`` (PUSH_ALLOW_REF_DELIMITER): ``:`` is
          already taken inside the repo half by SSH remote URLs
          (``git@host:group/proj``) and by ``https://host:port``, and ``,``
          separates entries, so ``|`` is the unambiguous choice.

        Backward-compatibility rule: a BARE entry authorizes branch refs
        only (``refs/heads/*``). Tag pushes must be granted explicitly with
        ``repo|refs/tags/*`` — no pre-existing allow list silently gains
        tag-push rights.

        Malformed entries — empty repo half, empty ref half, or more than
        one delimiter — return None and are IGNORED by the caller: an
        unparseable grant must never fail open and widen what is allowed.
        """
        if PUSH_ALLOW_REF_DELIMITER not in entry:
            return (entry, "refs/heads/*")
        parts = entry.split(PUSH_ALLOW_REF_DELIMITER)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return None
        return parts[0].strip(), parts[1].strip()

    def _normalize_remote_url(self, url: str) -> Optional[str]:
        """``host/path`` for a git remote URL, or None when it is not one.

        Anchored authorization (see _url_entry_authorizes) needs the two
        identity components that a substring test blurs together. The three
        forms git accepts are handled: ``scheme://[user[:pass]@]host[:port]/path``,
        scp-like ``[user@]host:path``, and bare ``host/path``. A trailing
        ``.git`` and surrounding slashes are dropped and the result is
        lowercased — host case is meaningless (DNS) and a path differing
        only in case is the same repository on every forge lmer targets,
        so folding case here removes a footgun instead of adding one.

        The parse is ANCHORED, and that is the whole point of this
        function. Userinfo may be stripped only where it can legally
        appear — inside the authority, i.e. before the first ``/`` of a
        ``scheme://`` URL, and before the host of the scp-like form. A
        naive ``rsplit("@", 1)`` over the whole string instead strips at
        the LAST ``@`` anywhere, so an attacker-chosen host carrying the
        allowed identity in its PATH normalizes to the allowed identity
        while git dials the attacker's host::

            https://evil.example.com/x@github.com/group/project
            git@evil.invalid:x@github.com/group/project.git

        Both must normalize to ``evil.example.com/…`` / ``evil.invalid/…``
        and be refused. ``urlsplit`` (which parses the authority, not the
        string) and the anchored scp regex — the same pair
        ``work_repo.git_ops._web_base_from_remote`` already uses — get this
        right by construction.

        None (= refuse) for anything without both a host and a path: a
        local filesystem path, a bare hostname, ``https://host/``, a URL
        whose path is only a fragment (``https://host/#@other/repo``) —
        none of them names a repository the allow-list grammar can
        authorize.
        """
        rest = url.strip()
        if "://" in rest:
            try:
                parts = urlsplit(rest)
            except ValueError:
                return None
            # `hostname` reads the authority: userinfo (last `@` WITHIN the
            # authority) and port are dropped by the parser, and a `#`/`?`
            # never leaks into the path.
            host, path = parts.hostname or "", parts.path
        else:
            # An IPv6 literal must be BRACKETED outside a `scheme://` URL
            # (git: "to avoid ambiguity with a local path containing a
            # colon"), and the bracket is the host's boundary — the
            # address's own colons are not the `host:path` delimiter. The
            # normalized host is the address without brackets, which is
            # what urlsplit reports for the scheme spelling, so every
            # spelling of one IPv6 repository is one identity. An
            # unclosed/empty/junk-trailing bracket names no host, exactly
            # as urlsplit refuses those.
            authority = rest
            userinfo = re.match(r"^[^@/]+@(\[.*)$", rest)
            if userinfo is not None:
                authority = userinfo.group(1)
            bracketed = (authority.startswith("[")
                         or "]" in authority.split("/", 1)[0])
            match = None if bracketed else re.match(
                # scp-like `[user@]host:path`: userinfo may contain neither
                # `@` nor `/`, and the host neither `:` nor `/` — so the
                # `@` and the `:` this matches are the real delimiters,
                # never ones sitting inside the path.
                r"^(?:[^@/]+@)?([^:/]+):(.+)$", rest)
            if bracketed:
                bracket = re.fullmatch(r"\[([^\[\]/]+)\](?:[:/](.*))?",
                                       authority)
                if bracket is None:
                    return None
                host, path = bracket.group(1), bracket.group(2) or ""
            elif match is not None:
                host, path = match.group(1), match.group(2)
            else:
                # Bare `host/path`. No userinfo is legal here, so a host
                # carrying `@` or `:` is malformed — refuse rather than
                # guess at which half was meant to be the identity.
                host, _, path = rest.partition("/")
                if not re.fullmatch(r"[^@/:]+", host):
                    return None
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[:-len(".git")]
        if not host or not path:
            return None
        return f"{host}/{path}".lower()

    def _url_entry_authorizes(self, entry: str, remote_url: str) -> bool:
        """Anchored allow-list match for a push-by-URL target.

        The plain ``repo in remote_url`` substring rule is sound only for
        OPERATOR-configured remotes. On the push-by-URL branch the string
        being matched is whatever ``--remote`` carried on the command line,
        and an unanchored substring then authorizes any host that merely
        embeds the allowed path: ``agents/global`` would authorize
        ``https://evil.example.com/mirror/agents/global.git``.

        So the URL is parsed and the entry must name one of TWO anchored
        identities, both of which pin the HOST: the full ``host/path``, or
        the bare ``host``. A partial path component (``global``,
        ``agents/gl``) authorizes nothing, and an unparseable URL refuses.

        A path-only entry (``group/project``) deliberately does NOT
        authorize here. Every forge lets anyone serve the same path, so
        matching a bare path against an agent-supplied URL is the very
        substring hole this function exists to close, only spelled with a
        different prefix: ``group/project`` would authorize
        ``https://evil.example.com/group/project.git``. Operators who want
        a path-only grant still have one for CONFIGURED remotes (see
        run_push_gate — that URL came from the operator's own git config);
        for push-by-URL the entry must say which host.

        The #107 grammar additions (wildcard domains, host+project
        prefixes) are deliberately INERT here for the same reason: neither
        names the single host git will dial. Delegates to
        ``push_allow.entry_allows(..., exact_identity=True)``, which is the
        one implementation of that rule.
        """
        if self._normalize_remote_url(remote_url) is None:
            # Not a repository identity at all (filesystem path, bare host,
            # fragment-only URL) — nothing to authorize.
            return False
        host, path = push_allow.split_target(remote_url)
        return push_allow.entry_allows(entry, host, path,
                                       exact_identity=True)

    def _resolve_push_target_ref(self, ref: str) -> Optional[str]:
        """The fully-qualified ref an explicit refspec lands on, or None.

        Authorization must key on what the push CHANGES on the remote — the
        ``<dst>`` side of a ``<src>:<dst>`` refspec — never on the whole
        refspec string: fnmatch's ``*`` crosses ``:`` and ``/``, so matching
        the raw refspec lets a branch-only grant (``refs/heads/*``) authorize
        ``refs/heads/main:refs/tags/v9.9``, which creates a remote TAG.
        Fail-closed rules (None = refuse, with the caller printing why):

        - a leading ``+`` (force push) is never authorized by the gate;
        - an EMPTY ``<src>`` (``:refs/heads/main``, ``:refs/tags/v0.5.0``)
          is a DELETION refspec and is never authorized: deleting a remote
          ref is at least as destructive as the force push refused above,
          and the release flow declares published tags immutable (never
          deleted, never re-pointed). Deleting a ref stays a human
          decision, made with plain git;
        - the dst side must be fully qualified (``refs/...``): a short name
          is resolved by git against the remote's refs (a short ``v1.2.3``
          becomes ``refs/tags/v1.2.3`` when such a tag exists), so
          normalizing it to ``refs/heads/<name>`` here would authorize the
          wrong ref class;
        - glob characters in the ref are refused — the gate cannot soundly
          match a pattern against a pattern.

        Only ``ref=None`` (the current-branch default, resolved from
        ``git branch --show-current`` by the caller) may be normalized with
        ``refs/heads/`` — there the branch identity is authoritative.
        """
        if ref.startswith("+"):
            return None
        if any(ch in ref for ch in "*?["):
            return None
        if ":" in ref:
            src, _, dst = ref.partition(":")
            if ":" in dst or not dst or not src.strip():
                return None
            ref = dst
        if not ref.startswith("refs/"):
            return None
        return ref

    def _push_repo_half_grants(self, repo: str, url: str,
                               by_url: bool) -> bool:
        """Repo half of one allow-list entry against one push URL.

        The two branches are NOT the same rule. Push-by-URL keeps the
        pre-#107 anchored check verbatim (_url_entry_authorizes, which also
        REFUSES a string that is not a repository identity at all —
        ``user@host/path``, ``https://host/`` with no path). A configured
        remote — a URL the operator put in git config — gets the #107
        grammar. Routing both through the grammar would quietly widen the
        adversarial branch, since the grammar accepts entry shapes
        (host-less paths, wildcard domains, prefixes) that name no single
        host for git to dial.
        """
        if by_url:
            return self._url_entry_authorizes(repo, url)
        host, path = push_allow.split_target(url)
        if not host or not path:
            return False  # unidentifiable target: fail closed
        return push_allow.entry_allows(repo, host, path)

    def _push_granting_source(
            self, url: str, target_ref: str,
            sources: List[Tuple[List[str], str]],
            by_url: bool) -> Optional[Tuple[str, str]]:
        """The (entry, source) that authorizes ``url`` for ``target_ref``.

        BOTH halves of an entry must grant: the repo half through the
        push_allow grammar (#107), the ref half through the fnmatch pattern
        (bare entries default to refs/heads/*). Checking them together per
        entry — rather than "some entry matches the repo and some entry
        matches the ref" — is what keeps a branch-only grant for repo A
        from authorizing a tag push to repo B.
        """
        for entries, source in sources:
            for entry in entries:
                parsed = self._parse_push_allow_entry(entry)
                if parsed is None:
                    continue  # malformed: ignored, never fails open
                repo, ref_pattern = parsed
                if not fnmatch.fnmatch(target_ref, ref_pattern):
                    continue
                if self._push_repo_half_grants(repo, url, by_url):
                    return entry, source
        return None

    def _authorize_push_urls(
            self, remote_urls: List[str], target_ref: str,
            sources: List[Tuple[List[str], str]],
            by_url: bool) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
        """``(grants, denied)`` over every push URL of the remote.

        EVERY push URL must be granted: git sends the ref to all of them,
        so one unallowlisted pushurl is one unauthorized push.

        The refusals are collected as a LIST rather than inferred from the
        grants dict. A remote may carry the SAME pushurl twice (``git
        remote set-url --add --push`` run twice — a re-run setup script),
        and a dict keyed by URL then holds one entry for two list members,
        so ``len(grants) == len(remote_urls)`` could never be satisfied:
        the push would be refused however wide the allow list, with no URL
        marked as the reason.
        """
        grants: Dict[str, Tuple[str, str]] = {}
        denied: List[str] = []
        for url in remote_urls:
            granted = self._push_granting_source(
                url, target_ref, sources, by_url)
            if granted is not None:
                grants[url] = granted
            else:
                denied.append(url)
        return grants, denied

    def _report_push_refusal(self, remote: str, remote_urls: List[str],
                             denied: List[str], target_ref: str,
                             env_entries: List[str],
                             taskdef_entries: List[str],
                             taskdef_manifest: Optional[Path]) -> None:
        """Print why the push was refused: the checked target(s), which of
        them were not granted, every source consulted, and a
        copy-pasteable entry that would grant the first refused URL."""
        print(f"{Colors.RED}❌ Push not allowed to this repository{Colors.NC}")
        for url in remote_urls:
            # The workspace origin is a TOKENIZED clone URL
            # (https://oauth2:<token>@host/path.git), so printing it raw puts
            # a live credential on stdout and into the agent transcript
            # (#281). Authorization keeps reading the raw URL — only the
            # printed form is scrubbed.
            #
            # `remote` is scrubbed too: on the push-by-URL branch
            # (`gate-push --remote https://...`) it IS the tokenized URL,
            # so scrubbing only `url` left the credential in the
            # parenthetical. For a named remote ("origin") the scrub is a
            # no-op, so it is applied unconditionally.
            mark = "  <-- not allowed" if url in denied else ""
            print(f"Repository ({_scrub_credentials(remote)}): "
                  f"{_scrub_credentials(url)}{mark}")
        print(f"Target ref: {target_ref}")
        print("Sources checked:")
        if env_entries:
            print(f"  LMER_PUSH_ALLOW_LIST: {', '.join(env_entries)}")
        else:
            print("  LMER_PUSH_ALLOW_LIST: (not set)")
        if taskdef_entries:
            print(f"  taskdef task.yaml push_allow ({taskdef_manifest}): "
                  f"{', '.join(taskdef_entries)}")
        else:
            print("  taskdef task.yaml push_allow: (none declared)")
        if denied:
            host, path = push_allow.split_target(denied[0])
            if host and path:
                example = push_allow.example_entry(denied[0])
                # A bare entry authorizes branches only, so an example
                # offered for a TAG push must carry the ref half —
                # otherwise copy-pasting it earns a second refusal.
                ref_half = "" if target_ref.startswith("refs/heads/") \
                    else f"|{target_ref}"
                print("Example entry that would allow this push: "
                      f"{example}{ref_half}")
            else:
                # No host+path to name: the target is not a repository
                # identity the grammar can authorize (a filesystem remote,
                # say). Say so rather than printing an entry that cannot
                # be written.
                print("(The target does not parse into host/path, so no "
                      "allow-list entry can name it — push it with plain "
                      "git if that is really what you want.)")
        print("Get explicit permission before pushing.")

    def run_push_gate(self, ref: Optional[str] = None, remote: str = "origin") -> bool:
        """Run checks for push gate.

        ``ref`` is the ref being pushed. ``None`` means the current branch
        (normalized to ``refs/heads/<name>`` — the one safe normalization,
        since ``git branch --show-current`` is authoritative about being a
        branch; an EMPTY current branch, i.e. detached HEAD, resolves to
        nothing and refuses). An explicit ref must be fully qualified
        (``refs/heads/...``, ``refs/tags/...``) or a ``<src>:<dst>`` refspec
        with a fully qualified dst — the authorization keys on the dst side
        (see _resolve_push_target_ref). ``remote`` names the remote whose
        PUSH url (``get-url --push``, which is what git will actually dial)
        is checked against the allow list, so a mirror-repo entry authorizes
        pushes to that remote only. A ``remote`` git cannot resolve as a
        configured remote but that looks like a URL is gated against the URL
        itself, with an ANCHORED match (_url_entry_authorizes) rather than
        the substring rule the configured-remote branch keeps.

        Frozen flag names for bin/gate-push (R5): ``--tag NAME`` maps to
        ``ref="refs/tags/NAME"`` and ``--remote NAME`` maps to
        ``remote=NAME``.

        The allow list is the UNION of ``LMER_PUSH_ALLOW_LIST`` and the
        active taskdef's ``push_allow`` (trusted taskdef tiers only — never
        the agent-writable work-repo tiers). Each entry must grant with
        BOTH halves: repo (the :mod:`lmer_cli.push_allow` grammar) and ref
        (the fnmatch pattern, branch-only unless stated). Every push URL of
        the remote must be granted, and both the grant and the refusal name
        the target, the granting entry and its source (#107).
        """
        # `--push`, NOT the bare form: `git remote get-url <remote>` returns
        # the FETCH url, while `git push <remote>` uses
        # `remote.<name>.pushurl` whenever it is configured. Gating the
        # fetch url would authorize one repository and push to another —
        # one `git config remote.origin.pushurl <url>` in a target-repo
        # checkout would be enough to send a signed release tag elsewhere
        # with the gate green. `--push` falls back to the fetch url when no
        # pushurl is set, so this is the same answer git itself will use.
        #
        # `--all`, because a remote may carry SEVERAL pushurls and `git push`
        # sends to every one of them. Without it git prints only the first,
        # so a second, unallowlisted pushurl would receive the push with the
        # gate green (#107).
        code, stdout, _ = self.run_command(
            ["git", "remote", "get-url", "--push", "--all", remote])
        by_url = False

        if code == 0 and stdout.strip():
            remote_urls = [line.strip() for line in stdout.splitlines()
                           if line.strip()]
        elif any(marker in remote for marker in ("://", "@", "/")):
            # Push-by-URL (`gate-push --remote https://...`): gate on the URL
            # itself, so a raw-URL push faces exactly the same allow list as
            # a named remote instead of skipping it. The match is ANCHORED
            # here (_url_entry_authorizes) — this URL is agent-supplied, and
            # a substring rule written for operator-configured remotes would
            # authorize any host embedding the allowed path.
            remote_urls = [remote]
            by_url = True
        else:
            # A named remote git cannot resolve: FAIL CLOSED. The previous
            # behavior (skip the allow-list check and let the push surface
            # git's error) let `--remote <anything>` bypass the list.
            print(f"{Colors.RED}❌ Cannot resolve remote '{remote}' — "
                  f"refusing to push (fail closed){Colors.NC}")
            return False

        target_ref = ref
        if target_ref is None:
            bcode, bout, _ = self.run_command(["git", "branch", "--show-current"])
            branch = bout.strip() if bcode == 0 else ""
            if not branch:
                # Detached HEAD: `git branch --show-current` exits 0 with
                # EMPTY stdout. Interpolating that yields the literal
                # "refs/heads/", and fnmatch("refs/heads/", "refs/heads/*")
                # is True — `*` matches empty — so any bare allow-list entry
                # would authorize a ref that names no branch at all. There
                # is nothing to authorize here: refuse, exactly as for a
                # non-zero exit.
                print(f"{Colors.RED}❌ Cannot resolve the current branch "
                      f"(detached HEAD?) — refusing to push (fail "
                      f"closed){Colors.NC}")
                print("Push an explicit fully-qualified ref instead "
                      "(gate-push --ref refs/heads/<name>).")
                return False
            target_ref = f"refs/heads/{branch}"
        else:
            resolved = self._resolve_push_target_ref(target_ref)
            if resolved is None:
                print(f"{Colors.RED}❌ Refusing to authorize ref "
                      f"'{target_ref}'{Colors.NC}")
                print("The gate authorizes only fully-qualified refs "
                      "(refs/heads/..., refs/tags/...) or <src>:<dst> "
                      "refspecs with a fully-qualified dst; force-push (+), "
                      "deletion (:<dst>) and glob refspecs are never "
                      "authorized.")
                return False
            target_ref = resolved

        env_entries = push_allow.env_allow_list()
        taskdef_entries, taskdef_manifest = push_allow.taskdef_allow_source()
        sources = [(env_entries, "LMER_PUSH_ALLOW_LIST"),
                   (taskdef_entries, f"task.yaml @ {taskdef_manifest}")]

        grants, denied = self._authorize_push_urls(
            remote_urls, target_ref, sources, by_url)

        if denied or not remote_urls:
            self._report_push_refusal(remote, remote_urls, denied, target_ref,
                                      env_entries, taskdef_entries,
                                      taskdef_manifest)
            return False

        # Success is as transparent as refusal: name the entry that granted
        # each push URL and where that entry came from. A grant arriving
        # from a taskdef manifest rather than the operator's env is exactly
        # the case worth seeing.
        for url in remote_urls:
            entry, source = grants[url]
            # Scrubbed for the same reason as the refusal above (#281): the
            # granted URL is the tokenized one git will dial, and on the
            # push-by-URL branch `remote` carries that same URL.
            print(f"{Colors.GREEN}✅ Push target allowed{Colors.NC} "
                  f"({_scrub_credentials(remote)}): {_scrub_credentials(url)} "
                  f"[{target_ref}]")
            print(f"   granted by '{entry}' from {source}")

        # What a push changes is the commit range, so hand the tests check a
        # base to diff against (#269). Only the current-branch default earns
        # one: _changed_paths diffs the base against HEAD, and HEAD is the
        # range being pushed ONLY when the push is "this branch". An explicit
        # `--ref` may name another branch, or a `<src>:<dst>` refspec whose
        # src is another branch — bin/gate-push hands that refspec to git
        # verbatim — so classifying HEAD there would narrow the suite for a
        # diff the push does not contain. Any explicit ref (branch, tag or
        # refspec) therefore leaves the base unset and the fast path off.
        self.in_push_gate = True
        if ref is None and not by_url:
            self.push_diff_base = f"{remote}/{target_ref[len('refs/heads/'):]}"

        # Run commit gate checks first
        return self.run_commit_gate()


def commit_gate(verbose: bool = False, debug: bool = False,
                gate: Optional[GateSystem] = None) -> int:
    """Entry point for commit gate.

    Callers that need the run's results afterwards (e.g. bin/gate-check
    building its receipt summary) pass their own GateSystem via `gate`.
    """
    if gate is None:
        gate = GateSystem(verbose=verbose, debug=debug)
    if gate.run_commit_gate():
        print(f"\n{Colors.GREEN}You may proceed with commit.{Colors.NC}")
        return 0
    else:
        print(f"\n{Colors.RED}Please fix issues before committing.{Colors.NC}")
        return 1


def push_gate(verbose: bool = False, gate: Optional[GateSystem] = None) -> int:
    """Entry point for push gate.

    Like commit_gate, accepts a caller-owned GateSystem so bin/gate-push can
    read the results back for its receipt summary.
    """
    if gate is None:
        gate = GateSystem(verbose=verbose)
    if gate.run_push_gate():
        print(f"\n{Colors.GREEN}You may proceed with push.{Colors.NC}")
        return 0
    else:
        print(f"\n{Colors.RED}Push blocked. Fix issues first.{Colors.NC}")
        return 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gate system for enforcing development rules")
    parser.add_argument('command', choices=['commit', 'push'], help='Gate to run')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')

    args = parser.parse_args()

    if args.command == 'commit':
        sys.exit(commit_gate(args.verbose))
    elif args.command == 'push':
        sys.exit(push_gate(args.verbose))
