#!/Agents/global/.venv/bin/python3
"""
Hook for rgr-* (focused rule reading) commands.
Ensures specific rule modules are read and highlights key points.
"""
import sys
import time
from pathlib import Path
import json


# Mapping of commands to rule files and key sections
COMMAND_MAP = {
    'rgr-git': {
        'file': 'rules/git.md',
        'name': 'Git & Version Control',
        'key_points': [
            '🚨 NEVER push without permission',
            '🚨 NEVER commit without review',
            '📋 Check repository allow list',
            '🔍 Run git remote -v before push'
        ]
    },
    'rgr-test': {
        'file': 'rules/testing.md',
        'name': 'Testing & Quality Assurance',
        'key_points': [
            '🚨 ALWAYS use pytest (never unittest)',
            '🚨 ALWAYS wait for tests to pass',
            '📋 Add tests for all fixes',
            '🔍 Use fixtures properly'
        ]
    },
    'rgr-code': {
        'file': 'rules/code-quality.md',
        'name': 'Code Quality & Style',
        'key_points': [
            '🚨 ALWAYS run linters before commit',
            '🚨 NEVER leave debug statements',
            '📋 Follow existing code style',
            '🔍 Keep functions under 50 lines'
        ]
    },
    'rgr-security': {
        'file': 'rules/security.md',
        'name': 'Security Practices',
        'key_points': [
            '🚨 NEVER expose secrets in code',
            '🚨 NEVER commit credentials',
            '📋 Use environment variables',
            '🔍 Validate all inputs'
        ]
    },
    'rgr-docs': {
        'file': 'rules/documentation.md',
        'name': 'Documentation Standards',
        'key_points': [
            '🚨 ALWAYS document new features',
            '🚨 ALWAYS write clear docstrings',
            '📋 Include usage examples',
            '🔍 Keep docs up to date'
        ]
    },
    'rgr-ci': {
        'file': 'rules/ci-cd.md',
        'name': 'CI/CD & Monitoring',
        'key_points': [
            '🚨 ALWAYS wait for CI checks',
            '🚨 NEVER declare PR ready without verification',
            '📋 Monitor with actual delays',
            '🔍 Fix failures before proceeding'
        ]
    },
    'rgr-deps': {
        'file': 'rules/dependencies.md',
        'name': 'Dependency Management',
        'key_points': [
            '🚨 ALWAYS check before adding deps',
            '🚨 ALWAYS use lock files',
            '📋 Pin versions for reproducibility',
            '🔍 Review security advisories'
        ]
    }
}


def get_command_from_args():
    """Extract the rgr-* command from arguments."""
    if len(sys.argv) < 2:
        print("❌ ERROR: No command specified")
        print("💡 Usage: rgr_focused.py <command>")
        print(f"   Available: {', '.join(COMMAND_MAP.keys())}")
        return None

    return sys.argv[1]


def read_focused_rules(command):
    """Read and display focused rules."""
    if command not in COMMAND_MAP:
        print(f"❌ ERROR: Unknown command '{command}'")
        print(f"💡 Available commands: {', '.join(COMMAND_MAP.keys())}")
        return False

    config = COMMAND_MAP[command]

    # Check container/mounted locations first
    lmer_global = Path("/home/developer/.lmer")
    agents_global = Path("/Agents/global")

    if lmer_global.exists():
        base_path = lmer_global
    elif agents_global.exists():
        base_path = agents_global
    else:
        # Fallback to home directory (for dev/host environments)
        base_path = Path.home() / "Agents/global"

    rules_path = base_path / config['file']

    if not rules_path.exists():
        print(f"❌ ERROR: Rule file not found: {config['file']}")
        return False

    # Record focused read
    timestamp_file = Path.home() / ".claude/last_rgr_timestamp"
    timestamp_file.parent.mkdir(exist_ok=True)

    with open(timestamp_file, 'w') as f:
        json.dump({
            "timestamp": time.time(),
            "file": str(rules_path),
            "command": command,
            "focused": True
        }, f)

    print(f"📚 Reading {config['name']} Rules")
    print("=" * 50)
    print(f"📍 File: {config['file']}")

    # Show key points
    print("\n🎯 Key Points to Remember:")
    for point in config['key_points']:
        print(f"   {point}")

    # Extract and show critical section
    with open(rules_path, 'r') as f:
        content = f.read()

    if "## 🚨 Critical" in content:
        print("\n🚨 Critical Rules from this module:")
        lines = content.split('\n')
        in_critical = False
        for line in lines:
            if "## 🚨 Critical" in line:
                in_critical = True
                continue
            elif line.startswith("##") and in_critical:
                break
            elif in_critical and line.strip().startswith("-"):
                print(f"   {line.strip()}")

    # Show gates if present
    if "## 🛑" in content:
        print("\n🛑 Gates in this module:")
        import re
        gates = re.findall(r'## 🛑 ([A-Z\s]+) GATE', content)
        for gate in gates:
            print(f"   - {gate} GATE")

    return True


def show_recent_violations(module):
    """Show violations specific to this module."""
    violations_file = Path.home() / ".claude/violations.log"
    if violations_file.exists():
        module_key = module.replace('rgr-', '')
        with open(violations_file, 'r') as f:
            violations = [v for v in f.readlines() if module_key in v.lower()]
            if violations:
                print(f"\n⚠️  Recent {module_key} violations:")
                for v in violations[-3:]:  # Last 3 relevant violations
                    print(f"   - {v.strip()}")


def main():
    """Main hook execution."""
    command = get_command_from_args()
    if not command:
        sys.exit(1)

    print(f"🔍 Executing {command} hook...\n")

    # Read focused rules
    if not read_focused_rules(command):
        sys.exit(1)

    # Show relevant violations
    show_recent_violations(command)

    # Module-specific reminders
    print("\n💡 Next Steps:")
    if command == 'rgr-git':
        print("   1. Check git status")
        print("   2. Verify repository against allow list")
        print("   3. Follow commit gate process")
    elif command == 'rgr-test':
        print("   1. Run existing tests first")
        print("   2. Write tests for your changes")
        print("   3. Use fixtures, not monkeypatch")
    elif command == 'rgr-security':
        print("   1. Scan for hardcoded secrets")
        print("   2. Check environment variable usage")
        print("   3. Review input validation")

    print(f"\n✅ {command} rules loaded successfully")


if __name__ == "__main__":
    main()
