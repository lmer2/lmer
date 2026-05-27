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
            )

        details = non_empty[-5:] if non_empty else ["Custom test runner failed - no output"]
        return CheckResult(
            name="Python Tests",
            status=CheckStatus.FAILED,
            message=f"Custom test runner failed ({script.name})",
            details=details,
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

        # Determine python executable - prefer venv if available
        venv_python = self.project_root / ".venv" / "bin" / "python"
        if venv_python.exists():
            python_cmd = str(venv_python)
        else:
            python_cmd = "python"

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
                message=f"All {test_count} passed"
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
                details=failures[:5]  # Show first 5 failures
            )

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

        if code == 0:
            return CheckResult(
                name="Pre-commit Hooks",
                status=CheckStatus.PASSED,
                message="All hooks passed"
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

        if not existing_changelogs:
            return CheckResult(
                name="Changelog",
                status=CheckStatus.WARNING,
                message="No changelog file found (e.g., CHANGELOG.yaml, CHANGES.md)",
                details=["Consider adding a changelog to communicate changes to users"],
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
            return CheckResult(
                name="Changelog",
                status=CheckStatus.WARNING,
                message=f"Changelog not updated (found: {', '.join(existing_changelogs)})",
                details=["Update the changelog if this commit includes user-facing changes"],
                is_critical=False
            )

        return CheckResult(
            name="Changelog",
            status=CheckStatus.PASSED,
            message=f"Changelog updated: {', '.join(staged_changelogs)}"
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

    def print_results(self):
        """Print all check results"""
        print(f"\n{Colors.BLUE}{'═' * 60}{Colors.NC}")
        print(f"{Colors.BLUE}{Colors.BOLD}                    GATE CHECK RESULTS{Colors.NC}")
        print(f"{Colors.BLUE}{'═' * 60}{Colors.NC}\n")

        failures = 0
        warnings = 0

        for result in self.results:
            # Choose color based on status
            if result.status == CheckStatus.PASSED:
                color = Colors.GREEN
            elif result.status == CheckStatus.FAILED:
                color = Colors.RED
                if result.is_critical:
                    failures += 1
            elif result.status == CheckStatus.WARNING:
                color = Colors.YELLOW
                warnings += 1
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

        return self.print_results()

    def _get_push_allow_list(self) -> list[str]:
        """Get the push allow list from LMER_PUSH_ALLOW_LIST env var.

        Returns an empty list if not configured (no repos auto-allowed).
        The env var should be a comma-separated list of repo path patterns.
        """
        allow_list_str = os.environ.get("LMER_PUSH_ALLOW_LIST", "")
        if not allow_list_str.strip():
            return []
        return [repo.strip() for repo in allow_list_str.split(",") if repo.strip()]

    def run_push_gate(self) -> bool:
        """Run checks for push gate"""
        # First check if we can push to this repo
        code, stdout, _ = self.run_command(["git", "remote", "-v"])

        if code == 0:
            allow_list = self._get_push_allow_list()

            remote_url = stdout.strip()
            allowed = any(repo in remote_url for repo in allow_list) if allow_list else False

            if not allowed:
                print(f"{Colors.RED}❌ Push not allowed to this repository{Colors.NC}")
                print(f"Repository: {remote_url}")
                if allow_list:
                    print(f"Allow list: {', '.join(allow_list)}")
                else:
                    print("No repositories in allow list. Set LMER_PUSH_ALLOW_LIST env var.")
                print("Get explicit permission before pushing.")
                return False

        # Run commit gate checks first
        return self.run_commit_gate()


def commit_gate(verbose: bool = False, debug: bool = False) -> int:
    """Entry point for commit gate"""
    gate = GateSystem(verbose=verbose, debug=debug)
    if gate.run_commit_gate():
        print(f"\n{Colors.GREEN}You may proceed with commit.{Colors.NC}")
        return 0
    else:
        print(f"\n{Colors.RED}Please fix issues before committing.{Colors.NC}")
        return 1


def push_gate(verbose: bool = False) -> int:
    """Entry point for push gate"""
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
