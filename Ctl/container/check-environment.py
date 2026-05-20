#!/Agents/global/.venv/bin/python3
"""
Container Environment Checker
Detects if running in a container and verifies safety configurations.
"""

import os
import sys
import subprocess
from pathlib import Path


def get_bool_env(var_name: str, default: bool = False) -> bool:
    """
    Parse a boolean environment variable.

    Supports: "1", "yes", "true" (truthy) and "0", "no", "false" (falsy)
    """
    value = os.environ.get(var_name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "yes", "true"}:
        return True
    elif value in {"0", "no", "false"}:
        return False
    return default


class ContainerChecker:
    def __init__(self):
        self.is_container = False
        self.has_limits = False
        self.has_global_rules = False
        self.warnings = []
        self.info = []

    def check_container_markers(self):
        """Check for container environment markers."""
        # Check environment variables
        if os.environ.get('CLAUDE_CONTAINER') == 'true':
            self.is_container = True
            self.info.append("✓ CLAUDE_CONTAINER environment variable detected")

        # Check for container environment file
        if Path('/etc/container-environment').exists():
            self.is_container = True
            self.info.append("✓ Container environment file detected")
            with open('/etc/container-environment') as f:
                content = f.read()
                if 'RESOURCE_LIMITS' in content:
                    self.has_limits = True
                    self.info.append("✓ Resource limits configured")

        # Check for Docker/Podman
        if Path('/.dockerenv').exists():
            self.is_container = True
            self.info.append("✓ Docker environment detected")

        # Check cgroup for container signatures
        if Path('/proc/1/cgroup').exists():
            with open('/proc/1/cgroup') as f:
                if any('docker' in line or 'containerd' in line for line in f):
                    self.is_container = True
                    self.info.append("✓ Container cgroup detected")

    def check_resource_limits(self):
        """Check if resource limits are in place."""
        try:
            # Check ulimits
            result = subprocess.run(['ulimit', '-a'],
                                  capture_output=True,
                                  text=True,
                                  shell=True)
            if result.returncode == 0:
                limits = result.stdout
                if 'max user processes' in limits:
                    # Extract process limit
                    for line in limits.split('\n'):
                        if 'max user processes' in line:
                            proc_limit = line.split()[-1]
                            if proc_limit.isdigit() and int(proc_limit) <= 1024:
                                self.info.append(f"✓ Process limit: {proc_limit}")
                                self.has_limits = True
                            else:
                                self.warnings.append(f"⚠️ Process limit too high: {proc_limit}")
        except Exception as e:
            self.warnings.append(f"⚠️ Could not check ulimits: {e}")

    def check_global_rules(self):
        """Check if global rules are mounted."""
        # Check for global LMER installation first
        lmer_global_path = Path('/home/developer/.lmer')
        agents_global_path = Path('/Agents/global')

        global_found = False
        required_files = ['AGENTS.md', 'rules', 'hooks']

        # Check LMER global installation
        if lmer_global_path.exists():
            found = []
            for item in required_files:
                if (lmer_global_path / item).exists():
                    found.append(item)

            if found:
                global_found = True
                self.has_global_rules = True
                self.info.append(f"✓ Global LMER installation found: {', '.join(found)}")

        # Check traditional /Agents/global mount
        elif agents_global_path.exists():
            found = []
            for item in required_files:
                if (agents_global_path / item).exists():
                    found.append(item)

            if found:
                global_found = True
                self.has_global_rules = True
                self.info.append(f"✓ Global rules mounted: {', '.join(found)}")
            else:
                self.warnings.append("⚠️ /Agents/global exists but missing key files")

        if not global_found:
            if not get_bool_env('LMER_ALLOW_UNSAFE', default=False):
                raise RuntimeError("❌ ERROR: No global rules found - refusing to run without safety rules! Set LMER_ALLOW_UNSAFE=1 (or yes/true) to override.")
            else:
                self.warnings.append("⚠️ WARNING: No global rules found - running without safety rules! (LMER_ALLOW_UNSAFE is set)")

    def check_workspace(self):
        """Check workspace configuration."""
        workspace = Path('/workspace')
        if workspace.exists():
            self.info.append("✓ Workspace directory available")
            # Check if it's writable
            test_file = workspace / '.write_test'
            try:
                test_file.touch()
                test_file.unlink()
                self.info.append("✓ Workspace is writable")
            except:
                self.warnings.append("⚠️ Workspace is read-only")
        else:
            self.warnings.append("⚠️ No /workspace directory")

    def print_report(self):
        """Print environment report."""
        print("=" * 60)
        print("🔍 CONTAINER ENVIRONMENT CHECK")
        print("=" * 60)

        if self.is_container:
            print("📦 Container Status: RUNNING IN CONTAINER")
        else:
            print("🖥️  Container Status: NOT IN CONTAINER (HOST SYSTEM)")

        print("\n📋 Environment Details:")
        for item in self.info:
            print(f"  {item}")

        if self.warnings:
            print("\n⚠️  Warnings:")
            for warning in self.warnings:
                print(f"  {warning}")

        print("\n🛡️  Safety Status:")
        safety_score = 0
        if self.is_container:
            print("  ✓ Running in container (isolated)")
            safety_score += 1
        else:
            print("  ✗ Not in container (UNSAFE)")

        if self.has_limits:
            print("  ✓ Resource limits configured")
            safety_score += 1
        else:
            print("  ✗ No resource limits (RISK OF FORK BOMB)")

        if self.has_global_rules:
            print("  ✓ Global rules available")
            safety_score += 1
        else:
            print("  ✗ Global rules not mounted")

        print(f"\n🎯 Safety Score: {safety_score}/3")

        if safety_score < 3:
            print("\n" + "⚠️ " * 10)
            print("WARNING: Not fully protected! Risk of resource exhaustion!")
            print("⚠️ " * 10)
            return 1
        else:
            print("\n✅ All safety measures in place!")
            return 0

def main():
    checker = ContainerChecker()
    checker.check_container_markers()
    checker.check_resource_limits()
    checker.check_global_rules()
    checker.check_workspace()
    return checker.print_report()

if __name__ == "__main__":
    sys.exit(main())
