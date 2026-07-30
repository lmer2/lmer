#!/Agents/global/.venv/bin/python3
"""
Hook for pc (Please Commit) command.
Enforces that commit gate was passed before allowing commit.
"""
import fnmatch
import os
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime


# LMER_PUSH_ALLOW_LIST extended grammar — DUPLICATED implementation.
#
# Decision (see also the pre-push heredoc in hooks/install.sh): this hook
# runs standalone in-container and cannot import lmer_cli (same constraint
# as container/clone_and_exec.py, cf. lmer_cli/tokens.py), so instead of
# delegating to `gate-push` it mirrors the allow-list check from
# lmer_cli.gates.GateSystem.run_push_gate / _parse_push_allow_entry.
# Keep the bodies in sync; the mirror is guarded by
# tests/test_push_allow_grammar_parity.py (#116).
def push_allowed(remote_url, ref, allow_list_str=None):
    """True if the allow list authorizes pushing `ref` to `remote_url`.

    Grammar (authoritative docs live in lmer_cli/gates.py): comma-separated
    entries, each either `repo` (substring of the remote URL, branch refs
    ONLY) or `repo|refpattern` (fnmatch against the fully-qualified ref,
    e.g. refs/tags/*). Malformed entries — empty half, more than one `|` —
    are IGNORED: an unparseable grant must never fail open.
    """
    if allow_list_str is None:
        allow_list_str = os.environ.get("LMER_PUSH_ALLOW_LIST", "")
    if not ref.startswith("refs/"):
        ref = f"refs/heads/{ref}"
    for entry in allow_list_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "|" not in entry:
            repo, ref_pattern = entry, "refs/heads/*"
        else:
            parts = entry.split("|")
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                continue
            repo, ref_pattern = parts[0].strip(), parts[1].strip()
        if repo in remote_url and fnmatch.fnmatch(ref, ref_pattern):
            return True
    return False


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
    matched_patterns = []

    for pattern in secret_patterns:
        if pattern.lower() in diff_lower:
            matched_patterns.append(pattern)

    if matched_patterns:
        print("\n⚠️  WARNING: Potential secrets detected in staged changes:")
        # `matched_pattern` holds a detector pattern name (e.g. "password="),
        # not a secret value — printing it is safe and intentional.
        for matched_pattern in matched_patterns:
            print(f"   - Pattern '{matched_pattern}' found")
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

    # Check if we should push (if on allow list). Authorization matches
    # gate-push: the remote URL comes from `git remote get-url --push
    # origin` — the PUSH url, since that is where git sends refs when
    # `remote.origin.pushurl` is set — and the ref is the current branch.
    # See push_allowed() above for the grammar and the mirrored-logic note.
    result = subprocess.run(
        ["git", "remote", "get-url", "--push", "origin"],
        capture_output=True, text=True
    )
    remote_url = result.stdout.strip()
    if result.returncode == 0 and remote_url:
        branch = subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip()

        # Detached HEAD: `--show-current` exits 0 with EMPTY stdout, and
        # push_allowed() would qualify that to the bare "refs/heads/",
        # which fnmatch matches against "refs/heads/*" (`*` matches empty)
        # — i.e. any bare allow-list entry would authorize a ref naming no
        # branch. There is nothing to authorize: say so and offer nothing.
        if not branch:
            print("\n⚠️  Cannot resolve the current branch (detached HEAD?)")
            print("   Not offering a push — check out a branch first")
        elif push_allowed(remote_url, branch):
            print(f"\n📍 Repository '{remote_url}' is on the allow list for this ref")
            response = input("Push to remote? (yes/no): ")
            if response.lower() == 'yes':
                # Push exactly what was authorized. A bare `git push`
                # resolves its remote through branch.<name>.pushRemote →
                # remote.pushDefault → branch.<name>.remote, so either of
                # the first two would send the commit to a remote the
                # allow-list check above never looked at — the same
                # authorize-one-target-dial-another shape as reading the
                # fetch url instead of the push url.
                subprocess.run(["git", "push", "origin", branch])
        else:
            print("\n⚠️  This repository/ref is not on the push allow list")
            print("   Get explicit permission before pushing")


if __name__ == "__main__":
    main()
