#!/Agents/global/.venv/bin/python3
"""
Hook for pc (Please Commit) command.
Enforces that commit gate was passed before allowing commit.
"""
import os
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime


def check_gate_passage():
    """Verify COMMIT GATE was recently passed."""
    gate_log = Path.home() / ".claude/gate_passages.log"

    if not gate_log.exists():
        print("❌ ERROR: No record of passing COMMIT GATE")
        print("💡 Run 'rgrpc' first to pass through the commit gate")
        return False

    # Check last gate passage
    with open(gate_log, 'r') as f:
        lines = f.readlines()
        if not lines:
            print("❌ ERROR: No gate passages recorded")
            return False

        last_passage = lines[-1].strip()
        # Parse timestamp
        try:
            timestamp_str = last_passage.split(" - ")[0]
            last_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')

            # Check if it was within last hour
            time_diff = datetime.now() - last_time
            if time_diff.total_seconds() > 3600:
                print("⚠️  WARNING: Last COMMIT GATE passage was >1 hour ago")
                print("💡 Consider running 'rgrpc' again to ensure all checks still pass")
                return False

        except Exception as e:
            print(f"⚠️  Could not parse gate passage time: {e}")

    return True


def check_staged_changes():
    """Verify there are staged changes to commit."""
    result = subprocess.run(
        ["git", "diff", "--staged", "--name-only"],
        capture_output=True,
        text=True
    )

    if not result.stdout.strip():
        print("❌ ERROR: No staged changes to commit")
        print("💡 Use 'git add' to stage your changes first")
        return False

    print("📋 Staged files:")
    for file in result.stdout.strip().split('\n'):
        print(f"   - {file}")

    return True


def verify_no_secrets():
    """Quick check for potential secrets in staged changes."""
    result = subprocess.run(
        ["git", "diff", "--staged"],
        capture_output=True,
        text=True
    )

    secret_patterns = [
        'password=',
        'api_key=',
        'token=',
        'secret=',
        'AWS_',
        'GITHUB_TOKEN',
        'private_key'
    ]

    diff_lower = result.stdout.lower()
    found_secrets = []

    for pattern in secret_patterns:
        if pattern.lower() in diff_lower:
            found_secrets.append(pattern)

    if found_secrets:
        print("\n⚠️  WARNING: Potential secrets detected in staged changes:")
        for secret in found_secrets:
            print(f"   - Pattern '{secret}' found")
        print("🔍 Please review your changes carefully!")

        # Don't block, just warn
        response = input("\nContinue anyway? (yes/no): ")
        if response.lower() != 'yes':
            return False

    return True


def create_commit():
    """Create the actual commit."""
    print("\n📝 Creating commit...")

    # Get commit message suggestion based on changes
    result = subprocess.run(
        ["git", "diff", "--staged", "--name-only"],
        capture_output=True,
        text=True
    )

    files = result.stdout.strip().split('\n')

    # Suggest commit message based on changes
    if 'test' in ' '.join(files).lower():
        suggestion = "Add tests for "
    elif any(f.endswith('.md') for f in files):
        suggestion = "Update documentation"
    elif 'hooks/' in ' '.join(files):
        suggestion = "Add enforcement hooks for shortcuts"
    else:
        suggestion = "Update "

    print(f"\n💡 Suggested commit message start: '{suggestion}...'")
    print("📝 Enter your commit message (or press Enter to cancel):")

    commit_msg = input("> ").strip()

    if not commit_msg:
        print("❌ Commit cancelled")
        return False

    # Create the commit
    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        print("\n✅ Commit created successfully!")
        print(result.stdout)

        # Log successful commit
        commit_log = Path.home() / ".claude/commits.log"
        with open(commit_log, 'a') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - Committed via pc: {commit_msg}\n")

        return True
    else:
        print("\n❌ Commit failed:")
        print(result.stderr)
        return False


def main():
    """Main hook execution."""
    print("🔍 Executing pc (Please Commit) hook...\n")

    # Check gate was passed
    if not check_gate_passage():
        sys.exit(1)

    # Check for staged changes
    if not check_staged_changes():
        sys.exit(1)

    # Check for secrets
    if not verify_no_secrets():
        sys.exit(1)

    # Create commit
    if not create_commit():
        sys.exit(1)

    # Check if we should push (if on allow list)
    result = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True)
    if result.stdout:
        allow_list_str = os.environ.get("LMER_PUSH_ALLOW_LIST", "")
        allowed_repos = [r.strip() for r in allow_list_str.split(",") if r.strip()]
        current_repo = None

        for line in result.stdout.split('\n'):
            for repo in allowed_repos:
                if repo in line:
                    current_repo = repo
                    break

        if current_repo:
            print(f"\n📍 Repository '{current_repo}' is on the allow list")
            response = input("Push to remote? (yes/no): ")
            if response.lower() == 'yes':
                subprocess.run(["git", "push"])
        else:
            print("\n⚠️  This repository is not on the push allow list")
            print("   Get explicit permission before pushing")


if __name__ == "__main__":
    main()
