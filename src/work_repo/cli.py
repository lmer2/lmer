"""Command-line interface for work repository management."""

from __future__ import annotations

import sys
import argparse
import os
from pathlib import Path
from datetime import datetime
import yaml

from .loggers import get_logger
from .info_reader import read_project_info
from .git_ops import commit_work_changes
from .memory import persist_memory, restore_memory
from .utils import redact_secrets, task_target_dir


def create_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Work Repository Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Read project info (concatenates all .md files from info directories)
  work read-project-info

  # Log a message to log.yaml
  work log "Task completed successfully"

  # Display last 50 lines of log file
  work log

  # Commit and push changes to work repository
  work commit

  # Commit with custom message
  work commit --message "Updated project logs"

  # Copy a report file to work repository
  work report --file report.md

  # Set a temporary goal/context
  work goal "description of current goal"

  # Display current goal
  work goal
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # read-project-info command
    read_info_parser = subparsers.add_parser(
        "read-project-info",
        help="Read and output project info from info directories",
    )

    # log command
    log_parser = subparsers.add_parser(
        "log",
        help="Log a message to log.yaml or display recent log entries",
    )
    log_parser.add_argument(
        "message",
        nargs="?",
        help="Log message to record (optional - if omitted, displays last 40 log entries)",
    )
    log_parser.add_argument(
        "--metadata",
        help="Additional metadata as key=value pairs (e.g., --metadata key1=value1 key2=value2)",
        nargs="*",
        default=[],
    )

    # commit command
    commit_parser = subparsers.add_parser(
        "commit",
        help="Commit and push changes to work repository",
    )
    commit_parser.add_argument(
        "--message",
        "-m",
        help="Commit message (defaults to auto-generated)",
    )

    # report command
    report_parser = subparsers.add_parser(
        "report",
        help="Copy a report file to work repository with timestamp",
    )
    report_parser.add_argument(
        "--file",
        "-f",
        required=True,
        help="Path to the report file to copy",
    )

    # goal command
    goal_parser = subparsers.add_parser(
        "goal",
        help="Set or display temporary context/goal",
    )
    goal_parser.add_argument(
        "description",
        nargs="?",
        help="Description of current goal (optional - if omitted, displays current goal)",
    )

    # memory command
    memory_parser = subparsers.add_parser(
        "memory",
        help="Restore or persist per-project agent memory (LMER_PERSIST_AGENT_MEMORY)",
    )
    memory_subparsers = memory_parser.add_subparsers(
        dest="memory_action", help="Memory action to perform"
    )
    memory_subparsers.add_parser(
        "restore",
        help="Restore saved agent memory from the work repo into Claude's memory dir",
    )
    memory_persist_parser = memory_subparsers.add_parser(
        "persist",
        help="Copy Claude's agent memory into the work repo, then commit and push",
    )
    memory_persist_parser.add_argument(
        "--message",
        "-m",
        help="Commit message (defaults to auto-generated)",
    )

    return parser


def cmd_read_project_info() -> int:
    """Execute read-project-info command."""
    try:
        content = read_project_info()
        print(content)
        return 0
    except Exception as e:
        print(f"❌ Error reading project info: {e}", file=sys.stderr)
        return 1


def render_truncated_log(log_file: Path, max_lines: int = 50) -> int:
    """
    Render the last N lines of a log file.

    Args:
        log_file: Path to the log file
        max_lines: Maximum number of lines to display (default: 50)

    Returns:
        Exit code (0 for success, 1 for error)
    """
    if not log_file.exists():
        print("No log entries found.")
        return 0

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except IOError as e:
        print(f"❌ Error reading log file: {e}", file=sys.stderr)
        return 1

    if not lines:
        print("No log entries found.")
        return 0

    # Display last N lines
    total_lines = len(lines)
    if total_lines > max_lines:
        print(f"(truncated to last {max_lines} lines)\n")
        lines = lines[-max_lines:]

    for line in lines:
        print(line, end='')

    return 0


def cmd_log(message: str | None, metadata: list[str]) -> int:
    """
    Execute log command.

    Args:
        message: Log message (optional - if None, displays recent log entries)
        metadata: List of key=value metadata pairs

    Returns:
        Exit code
    """
    try:
        logger = get_logger()

        # If no message provided, display recent log entries
        if message is None:
            # Get log file path from logger
            if not hasattr(logger, 'log_file'):
                print("❌ Cannot determine log file location", file=sys.stderr)
                return 1

            log_file = logger.log_file

            # Display log file location
            print(f"Log file: {log_file}")
            print()

            return render_truncated_log(log_file)

        # Validate message length
        MIN_MESSAGE_LENGTH = 20
        if len(message.strip()) < MIN_MESSAGE_LENGTH:
            print(f"❌ Log message is too short (minimum {MIN_MESSAGE_LENGTH} characters)", file=sys.stderr)
            print("\nUsage:", file=sys.stderr)
            print("  work log \"Your descriptive log message here\"", file=sys.stderr)
            print("  work log \"Your message\" --metadata key=value", file=sys.stderr)
            print("  work log  # (without message) displays recent log entries", file=sys.stderr)
            print(f"\nYour message ({len(message.strip())} characters): \"{message}\"", file=sys.stderr)
            return 1

        # Otherwise, log the message
        # Parse metadata
        metadata_dict = {}
        for item in metadata:
            if "=" in item:
                key, value = item.split("=", 1)
                metadata_dict[key] = value

        logger.log(message, metadata_dict if metadata_dict else None)
        # Redact the confirmation output too (logger already redacts what's written to file)
        print(f"✅ Logged: {redact_secrets(message)}")
        return 0
    except Exception as e:
        print(f"❌ Error logging message: {e}", file=sys.stderr)
        return 1


def cmd_commit(message: str | None) -> int:
    """
    Execute commit command.

    Args:
        message: Optional commit message

    Returns:
        Exit code
    """
    return commit_work_changes(message)


def cmd_report(file_path: str) -> int:
    """
    Execute report command.

    Copies a file to the work repository with a timestamped filename:
    {host}/{project}/{task_type}/{task_target}/{YYMMDD-HH-MM-SS.md}

    Args:
        file_path: Path to the report file to copy

    Returns:
        Exit code
    """
    try:
        # Build target directory path: {host}/{project}/{task_type}/{task_target}
        target_dir = task_target_dir()
        if target_dir is None:
            print("❌ LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
            return 1

        work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
        if not work_repo_path.exists():
            print(f"❌ Work repository not found at {work_repo_path}", file=sys.stderr)
            return 1

        # Validate source file exists
        source_file = Path(file_path)
        if not source_file.exists():
            print(f"❌ Report file not found: {file_path}", file=sys.stderr)
            return 1

        target_dir.mkdir(parents=True, exist_ok=True)

        # Generate timestamp filename: YYMMDD-HH-MM-SS.md
        now = datetime.now()
        timestamp = now.strftime("%y%m%d-%H-%M-%S")
        target_file = target_dir / f"{timestamp}.md"

        # Read, redact secrets, and write the file
        content = source_file.read_text(encoding="utf-8")
        redacted_content = redact_secrets(content)
        target_file.write_text(redacted_content, encoding="utf-8")

        print(f"✅ Copied report to: {target_file}")
        return 0
    except Exception as e:
        print(f"❌ Error copying report: {e}", file=sys.stderr)
        return 1


def cmd_goal(description: str | None) -> int:
    """
    Execute goal command.

    Sets or displays temporary context/goal. The goal is stored in a temporary file
    and is not persisted permanently (cleaned up on system restart).

    Args:
        description: Optional goal description. If provided, sets the goal.
                    If None, displays the current goal.

    Returns:
        Exit code
    """
    try:
        # Use a temporary file in /tmp for storing the goal
        # This persists across CLI invocations but is temporary (not permanent)
        goal_file = Path("/tmp") / "lmer_work_goal.txt"

        if description:
            # Set the goal
            goal_file.write_text(description, encoding="utf-8")
            print(f"✅ Goal set: {description}")
            return 0
        else:
            # Display current goal
            if goal_file.exists():
                current_goal = goal_file.read_text(encoding="utf-8").strip()
                if current_goal:
                    print(current_goal)
                    return 0

            print("No goal set", file=sys.stderr)
            return 0
    except Exception as e:
        print(f"❌ Error managing goal: {e}", file=sys.stderr)
        return 1


def cmd_memory(action: str | None, message: str | None, parser: argparse.ArgumentParser) -> int:
    """
    Execute memory command.

    Args:
        action: ``"restore"`` or ``"persist"`` (None prints help)
        message: Optional commit message (persist only)
        parser: The top-level parser, used to print help when no action is given

    Returns:
        Exit code
    """
    if action == "restore":
        return restore_memory()
    elif action == "persist":
        return persist_memory(message)
    else:
        parser.print_help()
        return 1


def main() -> int:
    """Main entry point for work CLI."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "read-project-info":
        return cmd_read_project_info()
    elif args.command == "log":
        return cmd_log(args.message, args.metadata)
    elif args.command == "commit":
        return cmd_commit(args.message)
    elif args.command == "report":
        return cmd_report(args.file)
    elif args.command == "goal":
        return cmd_goal(args.description)
    elif args.command == "memory":
        return cmd_memory(args.memory_action, getattr(args, "message", None), parser)
    else:
        print(f"❌ Unknown command: {args.command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
