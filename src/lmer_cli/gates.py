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
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any
from urllib.parse import urlsplit
from enum import Enum
import hashlib
import time

import yaml

from work_repo.utils import project_info_dir, task_info_dir


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


# Fixed, predictable location for the full gate-check log. Overwritten on every
# run so it always reflects the latest invocation. Lives under /tmp so it is
# never committed and needs no per-project configuration.
GATE_CHECK_LOG_PATH = Path("/tmp/gate-check.log")

# A pytest tail line ("1397 passed in 42.1s", "1 failed, 12 passed in 3s",
# "no tests ran in 0.01s" is intentionally NOT matched — no count, no claim).
PYTEST_SUMMARY_RE = re.compile(
    r"\b\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)\b"
)

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

# check_changelog() warning hints for repos with a changelog.d/ directory
CTL_FRAGMENT_HINT = "Or stage a fragment: changelog.d/YYYYMMDD-<topic>.yaml"
OTHER_TOOL_FRAGMENT_HINT = (
    "Or stage a changelog.d/ fragment in this repo's fragment convention"
)


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

    def __init__(self, verbose: bool = False, debug: bool = False):
        self.verbose = verbose
        self.debug = debug
        self.results: List[CheckResult] = []
        self.project_root = Path.cwd()
        self.timestamp = time.time()

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

        combined_output = (result.stdout or "") + (result.stderr or "")
        non_empty = [line for line in combined_output.splitlines() if line.strip()]

        if result.returncode == 0:
            # Surface a tail of the runner's output so a verbose `gate-check`
            # run can confirm something actually executed (the pytest path
            # gets this for free via its `N tests passed` summary parsing).
            details = non_empty[-5:] if non_empty else None
            return CheckResult(
                name="Python Tests",
                status=CheckStatus.PASSED,
                message=f"Custom test runner passed ({script.name})",
                details=details,
                full_output=combined_output,
            )

        details = non_empty[-5:] if non_empty else ["Custom test runner failed - no output"]
        return CheckResult(
            name="Python Tests",
            status=CheckStatus.FAILED,
            message=f"Custom test runner failed ({script.name})",
            details=details,
            full_output=combined_output,
        )

    def check_tests(self) -> CheckResult:
        """Run pytest tests, or a project-supplied custom test runner if present."""
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

        # Run pytest and capture output
        result = subprocess.run(
            [python_cmd, "-m", "pytest", "tests/", "-x", "--tb=short", "-q",
             "--ignore=tests/test_container_build.py"],
            capture_output=True,
            text=True,
            cwd=self.project_root,
            env=env,
        )

        code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        combined_output = stdout + stderr

        if code == 0:
            # Extract test count from pytest output
            test_count = "all tests"
            if "passed" in stdout:
                match = re.search(r'(\d+) passed', stdout)
                if match:
                    test_count = f"{match.group(1)} tests"

            return CheckResult(
                name="Python Tests",
                status=CheckStatus.PASSED,
                message=f"All {test_count} passed",
                full_output=combined_output,
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

            return CheckResult(
                name="Python Tests",
                status=CheckStatus.FAILED,
                message="Tests failed",
                details=failures[:5],  # Show first 5 failures
                full_output=combined_output,
            )

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
        """Run pre-commit hooks."""
        precommit_cmd = self._resolve_precommit_command()

        # Run pre-commit and capture output
        result = subprocess.run(
            precommit_cmd + ["run", "--all-files"],
            capture_output=True,
            text=True,
            cwd=self.project_root
        )

        code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        combined_output = stdout + stderr

        if code == 0:
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
        combined = (stdout + stderr).rstrip()
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

    def _load_secrets_ignore_patterns(self) -> List[str]:
        """Load secrets-check ignore globs from the work-repo project info.

        Reads `{LMER_WORK_REPO_PATH}/{LMER_REPO_HOST}/{LMER_REPO_PROJECT}/info/gate-check.yaml`
        and returns the `secrets.ignore` list. Returns an empty list if the
        file is absent or the env vars are not set — the config is optional.
        """
        info_dir = project_info_dir()
        if info_dir is None:
            return []

        config_path = None
        for candidate in ("gate-check.yaml", "gate-check.yml"):
            candidate_path = info_dir / candidate
            if candidate_path.is_file():
                config_path = candidate_path
                break

        if config_path is None:
            return []

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except (IOError, yaml.YAMLError):
            return []

        secrets_cfg = config.get("secrets") or {}
        ignore = secrets_cfg.get("ignore") or []
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
        when one is parseable from the tests check's captured output. None
        when neither exists — the receipt's `summary` field is then simply
        absent, never fabricated.
        """
        failed = [
            result.name for result in self.results
            if result.status == CheckStatus.FAILED and result.is_critical
        ]
        if failed:
            return "failed: " + ", ".join(failed)
        for result in self.results:
            if result.name == "Python Tests" and result.full_output:
                for line in reversed(result.full_output.splitlines()):
                    line = line.strip().strip("=").strip()
                    if PYTEST_SUMMARY_RE.search(line):
                        return line
        return None

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

    def run_commit_gate(self, skip_tests: bool = False) -> bool:
        """Run all checks for commit gate.

        When skip_tests=True, the long-running test suite is skipped. Callers
        are responsible for any caller-specific user-facing rationale (e.g.
        gate-commit prints a QUICK_GATE_COMMIT hint before invoking).
        """
        checks = [
            self.check_git_status,
            self.check_staged_files,  # Check for git add -A abuse
            self.check_branch,
            self.check_tests,
            self.check_precommit,
            self.check_secrets,
            self.check_code_quality,
            self.check_documentation,
            self.check_changelog,
            self.check_deliverable_formats,
            self.check_permissions,
        ]

        if skip_tests:
            print(f"{Colors.YELLOW}⚠️  Skipping Python Tests{Colors.NC}")
            # remove() compares by __eq__ — bound methods compare equal, and
            # this fails loudly with ValueError if check_tests is ever renamed
            # or removed, rather than silently making skip_tests a no-op.
            checks.remove(self.check_tests)

        for check in checks:
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

    def _get_push_allow_list(self) -> list[str]:
        """Get the push allow list from LMER_PUSH_ALLOW_LIST env var.

        Returns an empty list if not configured (no repos auto-allowed).
        The env var is a comma-separated list of entries; each entry is
        either a bare repo substring or ``repo|refpattern``:

        - ``repo`` is matched as a substring of the remote URL (unchanged
          from the original grammar).
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
        """
        allow_list_str = os.environ.get("LMER_PUSH_ALLOW_LIST", "")
        if not allow_list_str.strip():
            return []
        return [repo.strip() for repo in allow_list_str.split(",") if repo.strip()]

    def _parse_push_allow_entry(self, entry: str) -> Optional[Tuple[str, str]]:
        """Parse one allow-list entry into (repo_substring, ref_pattern).

        Bare entries get the branch-only default pattern ``refs/heads/*``
        (see _get_push_allow_list). Malformed entries — empty repo half,
        empty ref half, or more than one delimiter — return None and are
        IGNORED by the caller: an unparseable grant must never fail open
        and widen what is allowed.
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
            # scp-like `[user@]host:path`: userinfo may contain neither `@`
            # nor `/`, and the host neither `:` nor `/` — so the `@` and the
            # `:` this matches are the real delimiters, never ones sitting
            # inside the path.
            match = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", rest)
            if match is not None:
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
        """
        normalized = self._normalize_remote_url(remote_url)
        if normalized is None:
            return False
        candidate = entry.strip()
        if "://" in candidate or "@" in candidate:
            candidate = self._normalize_remote_url(candidate) or ""
        else:
            candidate = candidate.strip("/")
            if candidate.endswith(".git"):
                candidate = candidate[:-len(".git")]
            candidate = candidate.lower()
        if not candidate:
            return False
        host, _, _path = normalized.partition("/")
        return candidate in (normalized, host)

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
        """
        # `--push`, NOT the bare form: `git remote get-url <remote>` returns
        # the FETCH url, while `git push <remote>` uses
        # `remote.<name>.pushurl` whenever it is configured. Gating the
        # fetch url would authorize one repository and push to another —
        # one `git config remote.origin.pushurl <url>` in a target-repo
        # checkout would be enough to send a signed release tag elsewhere
        # with the gate green. `--push` falls back to the fetch url when no
        # pushurl is set, so this is the same answer git itself will use.
        code, stdout, _ = self.run_command(
            ["git", "remote", "get-url", "--push", remote])
        by_url = False

        if code == 0 and stdout.strip():
            remote_url = stdout.strip()
        elif any(marker in remote for marker in ("://", "@", "/")):
            # Push-by-URL (`gate-push --remote https://...`): gate on the URL
            # itself, so a raw-URL push faces exactly the same allow list as
            # a named remote instead of skipping it. The match is ANCHORED
            # here (_url_entry_authorizes) — this URL is agent-supplied, and
            # a substring rule written for operator-configured remotes would
            # authorize any host embedding the allowed path.
            remote_url = remote
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

        allow_list = self._get_push_allow_list()
        entries = [self._parse_push_allow_entry(e) for e in allow_list]

        def repo_matches(repo: str) -> bool:
            if by_url:
                return self._url_entry_authorizes(repo, remote_url)
            return repo in remote_url

        allowed = any(
            repo_matches(repo) and fnmatch.fnmatch(target_ref, ref_pattern)
            for repo, ref_pattern in (e for e in entries if e is not None)
        )

        if not allowed:
            print(f"{Colors.RED}❌ Push not allowed to this repository{Colors.NC}")
            print(f"Repository: {remote_url}")
            print(f"Target ref: {target_ref}")
            if allow_list:
                print(f"Allow list: {', '.join(allow_list)}")
            else:
                print("No repositories in allow list. Set LMER_PUSH_ALLOW_LIST env var.")
            print("Get explicit permission before pushing.")
            return False

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
