#!/Agents/global/.venv/bin/python3
"""
Hook for rgr (Read Global Rules) command.
Can read either the main AGENTS.md or specific rule modules.
Usage:
  rgr          - Read global AGENTS.md
  rgr git      - Read rules/git.md
  rgr test     - Read rules/testing.md
  rgr code     - Read rules/code-quality.md
  rgr security - Read rules/security.md
  rgr docs     - Read rules/documentation.md
  rgr ci       - Read rules/ci-cd.md
  rgr deps     - Read rules/dependencies.md
"""
import os
import sys
import time
from pathlib import Path
import subprocess
import json


# Mapping of arguments to rule files
RULE_MODULES = {
    'git': 'rules/git.md',
    'test': 'rules/testing.md',
    'testing': 'rules/testing.md',
    'code': 'rules/code-quality.md',
    'quality': 'rules/code-quality.md',
    'security': 'rules/security.md',
    'docs': 'rules/documentation.md',
    'documentation': 'rules/documentation.md',
    'ci': 'rules/ci-cd.md',
    'cd': 'rules/ci-cd.md',
    'ci-cd': 'rules/ci-cd.md',
    'deps': 'rules/dependencies.md',
    'dependencies': 'rules/dependencies.md'
}


def verify_rules_read(rule_module=None):
    """Verify that rules were actually read."""
    # Try to find global rules in order of preference
    # LMER_GLOBAL_DIR env var takes priority (used in tests and claude-runner.sh)
    env_global = os.environ.get("LMER_GLOBAL_DIR")
    lmer_global = Path("/home/developer/.lmer")
    agents_global = Path("/Agents/global")

    if env_global and Path(env_global).exists():
        base_path = Path(env_global)
    elif lmer_global.exists():
        base_path = lmer_global
    elif agents_global.exists():
        base_path = agents_global
    else:
        # Fallback to old path for compatibility
        base_path = Path.home() / "Agents/global"

    if rule_module:
        # Read specific rule module
        if rule_module.lower() not in RULE_MODULES:
            print(f"❌ ERROR: Unknown rule module '{rule_module}'")
            print("💡 Available modules: git, test, code, security, docs, ci, deps")
            return False

        rules_file = base_path / RULE_MODULES[rule_module.lower()]
        display_name = f"Rule Module: {RULE_MODULES[rule_module.lower()]}"
    else:
        # Read main AGENTS.md
        rules_file = base_path / "AGENTS.md"
        display_name = "Global Rules (AGENTS.md)"

    if not rules_file.exists():
        print(f"❌ ERROR: Rules file not found at {rules_file}")
        return False

    # Record that rules were accessed
    timestamp_file = Path.home() / ".claude/last_rgr_timestamp"
    timestamp_file.parent.mkdir(exist_ok=True)

    with open(timestamp_file, 'w') as f:
        json.dump({
            "timestamp": time.time(),
            "file": str(rules_file),
            "command": f"rgr {rule_module}" if rule_module else "rgr",
            "module": rule_module
        }, f)

    print(f"✅ {display_name} found and marked as read")
    print(f"📍 Location: {rules_file}")
    print("🔍 Key sections to review:")

    # Show critical sections
    with open(rules_file, 'r') as f:
        content = f.read()
        if "## 🚨 Quick Reference" in content:
            print("  - 🚨 Quick Reference - Critical Rules")
        if "## 🚨 Critical" in content:
            print("  - 🚨 Critical Rules")
        if "## 🛑 COMMIT GATE" in content:
            print("  - 🛑 COMMIT GATE - Mandatory commit checks")
        if "## 📋 Show Your Work Policy" in content:
            print("  - 📋 Show Your Work Policy")

        # For specific modules, show their unique sections
        if rule_module:
            import re
            gates = re.findall(r'## 🛑 ([A-Z\s]+) GATE', content)
            if gates:
                print("  - 🛑 Module-specific gates:")
                for gate in gates:
                    print(f"      • {gate} GATE")

    return True


def check_recent_violations():
    """Check if there were recent rule violations."""
    violations_file = Path.home() / ".claude/violations.log"
    if violations_file.exists():
        with open(violations_file, 'r') as f:
            violations = f.readlines()
            recent = [v for v in violations[-5:] if v.strip()]  # Last 5 violations
            if recent:
                print("\n⚠️  Recent rule violations detected:")
                for v in recent:
                    print(f"  - {v.strip()}")
                print("📖 Please pay special attention to these areas\n")


def main():
    """Main hook execution."""
    # Check if an argument was provided for a specific rule module
    rule_module = None
    if len(sys.argv) > 1:
        rule_module = sys.argv[1]
        print(f"🔍 Executing rgr hook for '{rule_module}' module...\n")
    else:
        print("🔍 Executing rgr (Read Global Rules) hook...\n")

    # Check for recent violations
    check_recent_violations()

    # Verify rules were read
    if not verify_rules_read(rule_module):
        sys.exit(1)

    # Remind about key rules
    print("\n💡 Remember:")
    if rule_module and rule_module.lower() in RULE_MODULES:
        # Module-specific reminders
        module = rule_module.lower()
        if module in ['git']:
            print("  - NEVER push without permission (check allow list)")
            print("  - NEVER commit without review and explicit approval")
            print("  - ALWAYS use gate commands (gate-check, gate-commit) — bare, not bin/gate-*")
        elif module in ['test', 'testing']:
            print("  - ALWAYS use pytest (never unittest)")
            print("  - ALWAYS wait for tests to pass")
            print("  - Add tests for all fixes")
        elif module in ['code', 'quality']:
            print("  - ALWAYS run linters before commit")
            print("  - NEVER leave debug statements")
            print("  - Follow existing code style")
        elif module in ['security']:
            print("  - NEVER expose secrets in code")
            print("  - NEVER commit credentials")
            print("  - Use environment variables for sensitive data")
        elif module in ['docs', 'documentation']:
            print("  - ALWAYS document new features")
            print("  - ALWAYS write clear docstrings")
            print("  - Include usage examples")
        elif module in ['ci', 'cd', 'ci-cd']:
            print("  - ALWAYS wait for CI checks")
            print("  - NEVER declare PR ready without verification")
            print("  - Monitor with actual delays")
        elif module in ['deps', 'dependencies']:
            print("  - ALWAYS check before adding deps")
            print("  - ALWAYS use lock files")
            print("  - Pin versions for reproducibility")
    else:
        # General reminders for global rules
        print("  - NEVER push without permission (check allow list)")
        print("  - NEVER commit without review and explicit approval")
        print("  - ALWAYS show your work (paste command outputs)")
        print("  - ALWAYS run tests and pre-commit before committing")

    print("\n✅ Hook completed. Rules have been marked as read.")
    print("📋 Next: Follow the rules in your implementation\n")


if __name__ == "__main__":
    main()
