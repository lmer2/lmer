"""Security-focused tests."""
import re
import subprocess
from pathlib import Path
import pytest


class TestSecurity:
    """Test security aspects of the system."""

    def test_no_hardcoded_secrets(self, project_root):
        """Scan for potential hardcoded secrets."""
        # Patterns that might indicate secrets
        secret_patterns = [
            r'(?i)(api[_-]?key|apikey)\s*=\s*["\'][\w\-]+["\']',
            r'(?i)(secret|password|passwd|pwd)\s*=\s*["\'][\w\-]+["\']',
            r'(?i)token\s*=\s*["\'][\w\-]+["\']',
            r'(?i)aws_access_key_id\s*=',
            r'(?i)aws_secret_access_key\s*=',
            r'private[_-]?key',
            r'BEGIN (RSA|DSA|EC|OPENSSH) PRIVATE KEY'
        ]

        # Files to check
        extensions = ['.py', '.md', '.yaml', '.yml', '.toml', '.json', '.sh']
        exclude_dirs = {'.git', '.venv', '__pycache__', 'node_modules', 'container-home'}

        found_issues = []

        for ext in extensions:
            for file_path in project_root.rglob(f'*{ext}'):
                # Skip excluded directories
                if any(excluded in file_path.parts for excluded in exclude_dirs):
                    continue

                try:
                    content = file_path.read_text()
                    for pattern in secret_patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            # Allow certain exceptions (examples, templates)
                            if 'example' in str(file_path).lower() or 'test' in str(file_path).lower():
                                continue
                            if any(word in content.lower() for word in ['example', 'template', 'dummy']):
                                continue
                            # Allow pattern definitions in security checking code
                            if 'hooks' in str(file_path) and 'secret_patterns' in content:
                                continue

                            found_issues.append(f"{file_path}: {pattern}")
                except Exception:
                    pass

        assert not found_issues, f"Potential secrets found:\n" + "\n".join(found_issues)

    def test_docker_security_settings(self, project_root):
        """Test Docker security configurations."""
        containerfile = project_root / "Containerfile"
        content = containerfile.read_text()

        # Security checks
        assert "USER developer" in content, "Container not running as non-root"
        assert "NOPASSWD:ALL" not in content or "# Only for development" in content

    def test_no_weak_crypto(self, all_rule_files):
        """Ensure no weak cryptographic algorithms are recommended."""
        # Use word boundaries to avoid false positives
        weak_algos = [
            (r'\bmd5\b', 'MD5'),
            (r'\bsha1\b', 'SHA1'),
            (r'\bdes\b', 'DES'),
            (r'\brc4\b', 'RC4')
        ]

        for rule_file in all_rule_files:
            content = rule_file.read_text().lower()

            for pattern, algo_name in weak_algos:
                # Check if mentioned without warning
                matches = re.findall(pattern, content)
                if matches:
                    # It's OK if it's in a "don't use" context
                    lines_with_algo = [l for l in content.split('\n') if re.search(pattern, l)]
                    for line in lines_with_algo:
                        assert any(word in line for word in ['never', 'don\'t', 'avoid', 'weak', 'deprecated']), \
                            f"{rule_file.name} mentions {algo_name} without warning: {line}"

    def test_git_hooks_security(self, project_root):
        """Test git hooks don't introduce security issues."""
        hooks_dir = project_root / "hooks"

        for hook in hooks_dir.glob("*.py"):
            content = hook.read_text()

            # No shell injection vulnerabilities
            if "subprocess.run" in content or "os.system" in content:
                # Check for proper argument handling
                assert "shell=True" not in content, f"{hook.name} uses shell=True (injection risk)"

            # No eval/exec
            assert "eval(" not in content, f"{hook.name} uses eval()"
            assert "exec(" not in content, f"{hook.name} uses exec()"

    def test_permission_checks(self, project_root):
        """Test file permissions are appropriate."""
        exclude_dirs = {'.git', '.venv', '__pycache__', 'node_modules'}

        # Executable files should have shebang
        for file_path in project_root.rglob("*.sh"):
            # Skip excluded directories
            if any(excluded in file_path.parts for excluded in exclude_dirs):
                continue
            with open(file_path) as f:
                first_line = f.readline()
                assert first_line.startswith("#!"), f"{file_path} missing shebang"

        for file_path in project_root.rglob("*.py"):
            # Skip excluded directories
            if any(excluded in file_path.parts for excluded in exclude_dirs):
                continue
            if file_path.stat().st_mode & 0o111:  # If executable
                with open(file_path) as f:
                    first_line = f.readline()
                    assert first_line.startswith("#!"), f"{file_path} is executable but missing shebang"
