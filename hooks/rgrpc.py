#!/Agents/global/.venv/bin/python3
"""
Hook for rgrpc (Read Global Rules Please Commit) command.
Ensures rules are read AND enforces commit gate before allowing commit.
"""
import sys
import subprocess
import time
from pathlib import Path
import json


def check_rules_were_read():
    """Verify rules were recently read."""
    timestamp_file = Path.home() / ".claude/last_rgr_timestamp"

    if not timestamp_file.exists():
        print("❌ ERROR: No record of reading global rules")
        print("💡 Run 'rgr' first to read the rules")
        return False

    with open(timestamp_file, 'r') as f:
        data = json.load(f)
        last_read = data.get('timestamp', 0)

    # Check if rules were read in last 24 hours
    if time.time() - last_read > 86400:
        print("⚠️  WARNING: Rules were last read >24 hours ago")
        print("💡 Consider running 'rgr' to refresh your knowledge")

    return True


def run_commit_gate():
    """Execute the COMMIT GATE checks."""
    print("\n🛑 COMMIT GATE - MANDATORY CHECKS")
    print("=" * 50)

    checks = {
        "tests": False,
        "pre_commit": False,
        "shown_results": False
    }

    # 1. Run tests
    print("\n1️⃣  Running tests...")
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/", "-x"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("✅ Tests passed!")
            checks["tests"] = True
            print(f"   Output: {result.stdout.splitlines()[-1] if result.stdout else 'All tests passed'}")
        else:
            print("❌ Tests failed!")
            print(result.stdout)
            print(result.stderr)
    except Exception as e:
        print(f"❌ Could not run tests: {e}")

    # 2. Run pre-commit
    print("\n2️⃣  Running pre-commit...")
    try:
        result = subprocess.run(
            ["pre-commit", "run", "--all-files"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("✅ Pre-commit checks passed!")
            checks["pre_commit"] = True
        else:
            print("⚠️  Pre-commit made changes or found issues:")
            print(result.stdout)
    except Exception as e:
        print(f"❌ Could not run pre-commit: {e}")

    # 3. Show results (we just did)
    checks["shown_results"] = True
    print("\n3️⃣  Results shown above ✅")

    # Summary
    print("\n📊 COMMIT GATE SUMMARY:")
    print(f"   Tests: {'✅' if checks['tests'] else '❌'}")
    print(f"   Pre-commit: {'✅' if checks['pre_commit'] else '❌'}")
    print(f"   Results shown: {'✅' if checks['shown_results'] else '❌'}")

    all_passed = all(checks.values())

    if all_passed:
        print("\n✅ All checks passed! Ready to commit.")
        print("4️⃣  Waiting for explicit approval...")
        print('   User should now type: "commit" or "please commit"')
    else:
        print("\n❌ COMMIT BLOCKED: Not all checks passed")
        print("   Fix the issues above and run 'rgrpc' again")

    return all_passed


def get_git_status():
    """Show current git status."""
    print("\n📋 Current git status:")
    result = subprocess.run(["git", "status", "--short"], capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    else:
        print("   No changes to commit")


def main():
    """Main hook execution."""
    print("🔍 Executing rgrpc (Read Global Rules Please Commit) hook...\n")

    # First check if rules were read
    if not check_rules_were_read():
        sys.exit(1)

    # Show git status
    get_git_status()

    # Run commit gate
    if not run_commit_gate():
        sys.exit(1)

    # Log successful gate passage
    gate_log = Path.home() / ".claude/gate_passages.log"
    gate_log.parent.mkdir(exist_ok=True)
    with open(gate_log, 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - COMMIT GATE passed via rgrpc\n")

    print("\n✅ Hook completed. COMMIT GATE passed.")
    print("⏳ Awaiting user approval to commit...\n")


if __name__ == "__main__":
    main()
