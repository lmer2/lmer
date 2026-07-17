"""Command-line interface for work repository management."""

from __future__ import annotations

import sys
import argparse
import hashlib
import os
import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from datetime import datetime
import yaml

from .loggers import get_logger
from .info_reader import read_project_info
from .git_ops import (
    commit_napkin_if_subdir,
    commit_work_changes,
    commit_work_path,
    push_napkin_if_separate,
    report_uncommitted_work_items,
    run_dir_push_status,
    web_url_for,
)
from . import goals, plan_index, run_state, specs_index
from .memory import persist_memory, restore_memory
from .utils import redact_secrets, task_target_dir

# `work verify` keeps only this much of the tail of the verified command's
# combined output in memory — enough to cover any runner's summary, bounded
# for arbitrarily chatty commands. The receipt's `output_tail_sha256` is the
# sha256 of exactly these bytes, so a receipt can be checked after the fact
# without the work repo ever storing the output itself.
VERIFY_TAIL_BYTES = 64 * 1024


# Where setup-workspace writes the routing env vars for the session to source.
# /tmp is container-local and session-scoped — never committed and never mounted
# from the host — so it can't leak into the host's ~/.lmer or into other sessions.
WORKSPACE_ENV_FILE = Path("/tmp/lmer-workspace-env.sh")


#: Overridable directory for LMER_ANSWER consume-once markers (mirrors the
#: patchable ``slack_chat.registry.REGISTRY_DIR`` convention). ``None`` means
#: /tmp — container-lifetime scoped, exactly like the env var itself. Tests
#: point it at a temp dir so markers never leak between runs. Consumed by
#: :func:`_answer_marker_path` (near ``cmd_session_start``).
ANSWER_MARKER_DIR: str | None = None


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

  # Copy a report file into the run dir (runs/<slug>/reports/)
  work report --file report.md

  # Seed a run for another slug (out-of-session run creation)
  work seed develop gate-receipts --goal "..." --name gate-receipts

  # Record a plan task's completion in the execution ledger
  work ledger set T2 --status done --commit 4a1f9c2 --receipt t2-tests

  # Show the ledger table
  work ledger

  # Set a temporary goal/context
  work goal "description of current goal"

  # Display current goal
  work goal

  # Bootstrap /workspace for dev work in a chat-mode session
  work setup-workspace https://git.example.com/group/project/-/issues/42
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
        help="Copy a report file into the run dir (reports/) with timestamp",
    )
    report_parser.add_argument(
        "--file",
        "-f",
        required=True,
        help="Path to the report file to copy",
    )

    # seed command (out-of-session run creation — issue #87 D3)
    seed_parser = subparsers.add_parser(
        "seed",
        help="Create a run for a slug other than the current session's",
    )
    seed_parser.add_argument(
        "taskdef",
        help="Taskdef of the new run (e.g. develop, review)",
    )
    seed_parser.add_argument(
        "target",
        help="Task target the slug derives from (URL, branch, SHA, or short token)",
    )
    seed_parser.add_argument(
        "--goal",
        help="Initial goal to record (appends a goal_set event)",
    )
    seed_parser.add_argument(
        "--name",
        help="Human-readable run name (kebab-case normalized; appends run_named)",
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
    goal_parser.add_argument(
        "--estimate-sessions", dest="estimate_sessions", type=int, metavar="N",
        help="Estimated sessions until solved, recorded with the goal (issue #99)",
    )
    goal_parser.add_argument(
        "--estimate-time", dest="estimate_time", metavar="STR",
        help='Estimated time until solved — free-form human string ("4h", "2d"), never parsed',
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

    # setup-workspace command
    setup_ws_parser = subparsers.add_parser(
        "setup-workspace",
        help="Clone + provision /workspace for a task target (chat-mode dev bootstrap)",
    )
    setup_ws_parser.add_argument(
        "target",
        help="Task target: an MR/PR/issue/work_items URL, or a plain repository URL",
    )
    setup_ws_parser.add_argument(
        "--task",
        default="develop",
        help="Task type for the work-repo layout / LMER_TASK (default: develop)",
    )
    setup_ws_parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip dependency sync (uv sync); only clone + provision docs",
    )

    # state command (run-state kernel — spec §4.3)
    state_parser = subparsers.add_parser(
        "state",
        help="Show or mutate the durable run state (state.yaml)",
    )
    state_parser.add_argument(
        "action", nargs="?", choices=["set"],
        help="'set' to mutate; omit to display current state",
    )
    state_parser.add_argument("--phase", help="Set the current phase (free-form)")
    state_parser.add_argument(
        "--stop-reason", dest="stop_reason",
        choices=["question", "yield", "complete", "critical_error", "none"],
        help="Why the session is stopping ('none' clears it)",
    )
    state_parser.add_argument(
        "--status", choices=["in-progress", "complete", "archived"],
        help="Run status",
    )
    state_parser.add_argument(
        "--critical-error", dest="critical_error",
        help='JSON object {"summary": ..., "detail": ...}; required with --stop-reason=critical_error',
    )
    state_parser.add_argument(
        "--question",
        help="The blocking question's text; only valid with (or after) --stop-reason=question",
    )

    # answer command (resume-on-answer — issue #98)
    answer_parser = subparsers.add_parser(
        "answer",
        help="Record the human's answer to the run's recorded open question",
    )
    answer_parser.add_argument(
        "text",
        help="The answer's text (clears open_question and the question stop)",
    )

    # name command (run naming — spec §1)
    name_parser = subparsers.add_parser(
        "name",
        help="Set or display the run's human-readable kebab-case name",
    )
    name_parser.add_argument(
        "value",
        nargs="?",
        help="Name to set (normalized to kebab-case; omit to display the current name)",
    )

    # verify command (gate receipts — issue #88 D2)
    verify_parser = subparsers.add_parser(
        "verify",
        help="Run a validation command and record a verify receipt event",
    )
    verify_parser.add_argument(
        "name",
        help="Receipt name matching the plan task's validation contract (e.g. tests)",
    )
    # dest must not be `command` — that is the top-level subcommand slot.
    verify_parser.add_argument(
        "verify_command",
        nargs=argparse.REMAINDER,
        metavar="-- command …",
        help="The command to run, after a `--` separator",
    )

    # event command
    event_parser = subparsers.add_parser(
        "event",
        help="Append an event to the run's events.jsonl",
    )
    event_parser.add_argument("type", help="Event type (e.g. review_posted)")
    event_parser.add_argument("--note", help="Short human-readable note")
    event_parser.add_argument("--data", help="JSON object with extra data")

    # ledger command (per-task execution ledger — issue #89)
    ledger_parser = subparsers.add_parser(
        "ledger",
        help="Show or mutate the per-task execution ledger (ledger.yaml)",
    )
    ledger_parser.add_argument(
        "action", nargs="?", choices=["set"],
        help="'set' to mutate; omit to display the ledger table",
    )
    ledger_parser.add_argument(
        "task_id", nargs="?", metavar="task-id",
        help="Plan task id (e.g. T3a); required with 'set'",
    )
    ledger_parser.add_argument(
        "--status", choices=list(run_state.TASK_STATUSES),
        help="Task status (required with 'set')",
    )
    ledger_parser.add_argument(
        "--title",
        help="Short task title (fields omitted on later writes are kept)",
    )
    ledger_parser.add_argument(
        "--commit",
        help="Project-repo commit sha the task landed as",
    )
    ledger_parser.add_argument(
        "--receipt",
        help="Name of the verify/gate receipt proving the task",
    )
    ledger_parser.add_argument("--note", help="Short free-form note")

    # plan command (plan-index lint — issue #90)
    plan_parser = subparsers.add_parser(
        "plan",
        help="Plan-index tooling (plan.index.json)",
    )
    plan_subparsers = plan_parser.add_subparsers(
        dest="plan_action", help="Plan action to perform"
    )
    plan_subparsers.add_parser(
        "check",
        help="Lint the run's plan.index.json (read-only): DAG acyclic, "
             "write-scopes disjoint, session_scope declared",
    )

    # goals command (frozen goal-sets — issue #91)
    goals_parser = subparsers.add_parser(
        "goals",
        help="Goal-set lifecycle for the run's goals.md "
             "(check / freeze / amend / assess)",
    )
    goals_parser.add_argument(
        "goals_action", nargs="?",
        choices=["check", "freeze", "amend", "assess"],
        help="Verb; omit to display the goal-set status",
    )
    goals_parser.add_argument(
        "--note",
        help="Context note for the recorded event (e.g. spec-approval context)",
    )
    goals_parser.add_argument(
        "--verdict", action="append", default=[],
        metavar="G<N>=<verdict>:<evidence>",
        help="assess only, repeatable — per-goal verdict "
             f"({'|'.join(goals.GOAL_VERDICTS)}) with its evidence",
    )

    # resume command
    resume_parser = subparsers.add_parser(
        "resume",
        help="Print the resume brief for the current run (read-only)",
    )
    resume_parser.add_argument("--json", action="store_true", dest="as_json",
                               help="Emit the decision as JSON")

    # artifact command
    artifact_parser = subparsers.add_parser(
        "artifact",
        help="Register a file as a durable run artifact (external sources are "
             "copied in; a source already inside the run dir is linked — #103)",
    )
    artifact_parser.add_argument("name", nargs="?",
                                 help="Artifact filename (e.g. spec.md)")
    artifact_parser.add_argument("--file", "-f",
                                 help="Source file to register (copied into the "
                                      "run dir, or linked when already there)")
    artifact_parser.add_argument(
        "--sync", action="store_true",
        help="Link masterplan bundle artifacts at the run-dir root (spec §6)",
    )

    # specs-index command (issue #101 — central specs directory)
    specs_index_parser = subparsers.add_parser(
        "specs-index",
        help="List the central specs index ({host}/{project}/specs/) or rebuild it from runs/",
    )
    specs_index_parser.add_argument(
        "--rebuild", action="store_true",
        help="Rebuild the index from runs/ (backfill for specs that predate it)",
    )

    # session-start / session-end (hook plumbing — spec §4.4)
    subparsers.add_parser(
        "session-start",
        help="Seed/claim the run for this session and print the resume brief (hook-facing)",
    )
    subparsers.add_parser(
        "session-end",
        help="Record session end, release the owner claim, push run state (hook-facing)",
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
        # If no message provided, display recent log entries
        if message is None:
            # Read-only path: prefer the run-dir log, fall back to the
            # legacy task-target location for pre-unification runs
            # (issue #87 D4 — the CLI never moves legacy files).
            rdir = run_state.run_dir()
            if rdir is None:
                print("❌ Cannot determine log file location", file=sys.stderr)
                return 1
            log_file = rdir / "log.yaml"
            if not log_file.exists():
                legacy_dir = task_target_dir()
                if legacy_dir is not None and (legacy_dir / "log.yaml").exists():
                    log_file = legacy_dir / "log.yaml"

            # Display log file location, with its web URL when derivable
            # (issue #104 — user-facing paths carry the clickable form).
            print(f"Log file: {log_file}")
            url = web_url_for(log_file)
            if url:
                print(f"Web: {url}")
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

        # Make sure the run exists first so log.yaml never lands in a
        # stateless dir. Fail-soft: a broken state layer must not block
        # logging — the entry still lands at the resolved run path.
        try:
            run_state.ensure_run()
        except Exception:
            pass

        logger = get_logger()
        logger.log(message, metadata_dict if metadata_dict else None)
        # Redact the confirmation output too (logger already redacts what's written to file)
        print(f"✅ Logged: {redact_secrets(message)}")
        return 0
    except Exception as e:
        print(f"❌ Error logging message: {e}", file=sys.stderr)
        return 1


def _sync_masterplan_links() -> list[str]:
    """Fail-soft masterplan artifact-link sync for the current run dir (spec §6).

    The kernel helper already never raises, but this wraps it in the same
    defensive style as the hook-facing commands: no sync problem may ever
    change the host command's behavior or exit code. Returns the link names
    now present at the run-dir root ([] when there is nothing to sync).
    """
    try:
        rdir = run_state.run_dir()
        if rdir is None or not rdir.is_dir():
            return []
        return run_state.sync_masterplan_artifacts(rdir)
    except Exception as exc:
        print(f"⚠️  masterplan artifact sync skipped: {exc}")
        return []


def cmd_commit(message: str | None) -> int:
    """
    Execute commit command (work repo, plus napkin in either mode).

    Args:
        message: Optional commit message

    Returns:
        Exit code (the work-repo commit result; napkin capture is best-effort)
    """
    # Self-maintain masterplan artifact links before staging (spec §6) so
    # every push carries the run-dir-root links. Fail-soft: never changes
    # the commit's behavior or exit code.
    _sync_masterplan_links()
    rc = commit_work_changes(message)
    # Napkin capture is best-effort in both modes and must never block the
    # worklog commit: a failure is logged but does not change the exit code.
    # Subdir mode needs its own staging pass — commit_work_changes stages only
    # the task-target and run-dir paths, never {work_repo}/napkin/.
    if commit_napkin_if_subdir(message) != 0:
        print("⚠️  napkin subdir commit failed (continuing); work-repo commit was unaffected", file=sys.stderr)
    if push_napkin_if_separate(message) != 0:
        print("⚠️  napkin push failed (continuing); work-repo commit was unaffected", file=sys.stderr)
    # Flag any stray untracked/unstaged files left behind — `work commit`
    # stages only the run dir, so a new info file elsewhere would otherwise
    # go unnoticed (issue #85). Runs after the napkin subdir commit so files
    # that pass just captured aren't flagged as strays. Fail-soft: the
    # commit's exit code stands.
    report_uncommitted_work_items()
    return rc


def cmd_report(file_path: str) -> int:
    """
    Execute report command.

    Copies a file into the run dir with a timestamped filename:
    {host}/{project}/runs/{slug}/reports/{YYMMDD-HH-MM-SS.md}
    (issue #87 D4 — the run dir is the single home for run output).

    Args:
        file_path: Path to the report file to copy

    Returns:
        Exit code
    """
    try:
        rdir = run_state.run_dir()
        if rdir is None:
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

        # Make sure the run exists so reports never land in a stateless
        # dir. Fail-soft: a state-layer problem must not lose the report.
        try:
            rdir, _ = run_state.ensure_run()
        except Exception:
            pass

        target_dir = rdir / "reports"
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
        url = web_url_for(target_file)
        if url:
            print(f"   Web: {url}")
        return 0
    except Exception as e:
        print(f"❌ Error copying report: {e}", file=sys.stderr)
        return 1


def cmd_goal(
    description: str | None,
    estimate_sessions: int | None = None,
    estimate_time: str | None = None,
) -> int:
    """
    Execute goal command.

    Sets or displays temporary context/goal. The goal is stored in a temporary file
    and is not persisted permanently (cleaned up on system restart).

    Args:
        description: Optional goal description. If provided, sets the goal.
                    If None, displays the current goal.
        estimate_sessions: Optional sessions-until-solved estimate (issue #99).
        estimate_time: Optional time-until-solved estimate — a free-form
                    human string ("4h", "2d"), stored verbatim, never parsed.

    Returns:
        Exit code
    """
    try:
        # Estimate flags (issue #99) only make sense while recording a goal,
        # and land only in the run state below — validate them up front.
        has_estimate = estimate_sessions is not None or estimate_time is not None
        if has_estimate and not description:
            print("❌ --estimate-sessions/--estimate-time require a goal description", file=sys.stderr)
            return 1
        if estimate_sessions is not None and estimate_sessions < 1:
            print("❌ --estimate-sessions must be a positive integer", file=sys.stderr)
            return 1
        if estimate_time is not None and not estimate_time.strip():
            print("❌ --estimate-time requires a non-empty string", file=sys.stderr)
            return 1

        # Use a temporary file in /tmp for storing the goal
        # This persists across CLI invocations but is temporary (not permanent)
        goal_file = Path("/tmp") / "lmer_work_goal.txt"

        if description:
            # Set the goal
            goal_file.write_text(description, encoding="utf-8")
            print(f"✅ Goal set: {description}")
            estimate = None
            if has_estimate:
                estimate = {"sessions": estimate_sessions, "time": estimate_time}
                print(f"✅ Estimate: {run_state.format_estimate(estimate)}")

            # Also record the goal durably in the run state when a run
            # context exists (spec §5.5). Fails soft — the legacy goal file
            # above is already written, and a state-layer problem must not
            # break `work goal`. The estimate (issue #99) rides along in
            # state.estimate and the goal_set event's data; a goal without
            # one keeps today's behavior byte-for-byte (no data payload).
            try:
                if run_state.run_dir() is not None:
                    rdir, state = run_state.ensure_run()
                    state["goal"] = description
                    # The estimate belongs to the goal it was recorded with:
                    # a new goal without estimate flags must not inherit the
                    # previous goal's estimate into the resume brief.
                    state["estimate"] = estimate
                    run_state.write_state(rdir, state)
                    run_state.append_event(
                        rdir, "goal_set", note=description,
                        data={"estimate": estimate} if estimate else None,
                    )
            except Exception:
                pass

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


def _write_workspace_env(result: dict) -> Path:
    """Write the derived LMER_* routing vars to a sourceable shell file.

    These four vars are what ``lmer <verb> <target>`` sets at container launch;
    a mid-session ``setup-workspace`` can't inject them into future shells, so
    they are persisted here for the session to ``source``. None are secret
    (host/project/task/target only — no tokens).
    """
    lines = [
        "# lmer workspace routing env — written by `work setup-workspace`.",
        "# Source this so `work log`/`commit`/`report` and gate-check's",
        "# work-repo-aware features can find this project:",
        f"#   source {WORKSPACE_ENV_FILE}",
        f"export LMER_REPO_HOST={shlex.quote(result['host'])}",
        f"export LMER_REPO_PROJECT={shlex.quote(result['project'])}",
        f"export LMER_TASK={shlex.quote(result['task'])}",
        f"export LMER_TASK_TARGET={shlex.quote(result['task_target'])}",
    ]
    WORKSPACE_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return WORKSPACE_ENV_FILE


def cmd_setup_workspace(target: str, task: str, sync_deps: bool) -> int:
    """Execute setup-workspace command.

    Bootstraps /workspace for a session that needs to do real dev work on a repo
    it was not started with (the chat-mode case in issue #69): clone, work-repo
    dirs, documentation provisioning, and dependency sync — the same setup a
    repo-targeted ``lmer`` session gets at container startup. Then writes the
    routing env vars for the session to source. Hard-errors if /workspace is
    already set up.

    Args:
        target: Task target (resource URL or plain repo URL).
        task: Task type for the work-repo layout / LMER_TASK.
        sync_deps: Run dependency sync when True.

    Returns:
        Exit code (0 success, 1 on any setup failure including already-set-up).
    """
    # Lazy import: only setup-workspace needs lmer_cli. Plain `work log`/`commit`
    # invocations should not pay to import the lmer_cli package.
    try:
        from lmer_cli.container.clone_and_exec import (
            setup_workspace,
            WorkspaceExistsError,
        )
    except ImportError as e:
        print(f"❌ Could not load workspace setup support: {e}", file=sys.stderr)
        return 1

    try:
        result = setup_workspace(target, task=task, sync_deps=sync_deps)
    except WorkspaceExistsError as e:
        print(f"❌ {e}", file=sys.stderr)
        print(
            "   Remove the existing /workspace contents first if you really "
            "want to re-create it.",
            file=sys.stderr,
        )
        return 1
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"❌ Workspace setup failed: {e}", file=sys.stderr)
        return 1

    # Restore this project's persisted agent memory now that setup_workspace has
    # set LMER_REPO_HOST/LMER_REPO_PROJECT in the environment. At normal container
    # startup this is done by claude-runner via `work memory restore` *after* the
    # entrypoint clones the repo; the chat->dev pivot never runs that runner step,
    # so do it here. No-op unless LMER_PERSIST_AGENT_MEMORY is enabled; best-effort
    # (the workspace is already set up, so a restore failure must not fail setup).
    try:
        restore_memory()
    except Exception as e:
        print(f"⚠️  Agent memory restore failed: {e}", file=sys.stderr)

    env_file = _write_workspace_env(result)

    provisioned = result.get("provisioned") or []
    provisioned_str = (
        ", ".join(provisioned) if provisioned else "none (project ships its own)"
    )

    print(f"✅ /workspace set up for {result['host']}/{result['project']}")
    print(f"   Branch:            {result.get('branch') or '(detached/unknown)'}")
    print(f"   Provisioned docs:  {provisioned_str}")
    print(f"   Dependencies:      {result.get('deps_status')}")
    print(f"   Task:              {result['task']}")
    print()
    print(
        "Routing vars for `work log`/`commit`/`report` and gate-check were "
        f"written to {env_file}."
    )
    print("Load them into your shell with:")
    print(f"   source {env_file}")
    print("Or export them directly:")
    print(f"   export LMER_REPO_HOST={shlex.quote(result['host'])}")
    print(f"   export LMER_REPO_PROJECT={shlex.quote(result['project'])}")
    print(f"   export LMER_TASK={shlex.quote(result['task'])}")
    print(f"   export LMER_TASK_TARGET={shlex.quote(result['task_target'])}")

    if str(result.get("deps_status", "")).startswith("FAILED"):
        # The workspace IS set up, so this is a warning rather than a failure
        # (mirrors container startup treating sync/provisioning as best-effort).
        print()
        print(
            "⚠️  Dependency sync failed — see the output above. The workspace "
            "is set up, but gate-check tests may fail until deps install."
        )

    return 0


def _require_run() -> tuple[Path, dict] | tuple[None, None]:
    """Resolve (and if needed create) the current run for mutations.

    Defensive auto-seed via run_state.ensure_run: taskdef instructions call
    `work state set` assuming the /start hook seeded the run — if it didn't
    (host session, older runner), the mutation must still land rather than
    fail. Prints the standard error and returns (None, None) when there is
    no run context or the state layer refuses.
    """
    if run_state.run_dir() is None:
        print("❌ No run context: LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return None, None
    try:
        return run_state.ensure_run()
    except (run_state.RunStateError, OSError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return None, None


def _push_run_dir(
    state: dict,
    detail: str,
    old_dir_name: str | None = None,
    saved: str = "state",
) -> None:
    """Durability push of the run dir after a mutation (non-fatal, like
    artifact writes). When a freeze-gate rename just happened, the
    pre-rename path is staged too so the old path's deletions land even
    when the rename-time push fails. `saved` names what stays local in the
    warning when the push fails."""
    rels = run_state.run_rel_path_candidates()
    if old_dir_name:
        host = os.environ.get("LMER_REPO_HOST")
        project = os.environ.get("LMER_REPO_PROJECT")
        old_rel = f"{host}/{project}/runs/{old_dir_name}"
        if old_rel not in rels:
            rels.append(old_rel)
    rc = commit_work_path(rels, f"run-state: {state['slug']} {detail}")
    if rc != 0:
        print(f"⚠️  Warning: run-state push failed ({saved} saved locally)")


def _advise_unpushed_phase_end(rdir: Path, old_phase: str, new_phase: str) -> None:
    """Pushed-deliverable advisory at a phase boundary (issue #100).

    A phase CHANGE ends a step, and every step must end with a pushed,
    linkable deliverable. The durability push that accompanies the
    transition normally guarantees that; this fires only when the run dir
    is STILL dirty or ahead of its upstream afterwards (the push failed —
    network down, rejected) — the same predicate the Stop-hook guard's
    trigger 2 uses (git_ops.run_dir_push_status). Loud advisory only: the
    exit code is untouched (fail-soft), and the run dir's web URL is
    included when derivable so the fix ends with a citable link.
    """
    try:
        dirty, unpushed = run_dir_push_status(rdir)
        if not (dirty or unpushed):
            return
        print(
            f"⚠️  phase '{old_phase}' ended with unpushed run-dir changes — "
            f"run `work commit` so the step's deliverable is pushed and "
            f"linkable before starting '{new_phase}'"
        )
        url = web_url_for(rdir)
        if url:
            print(f"   Run dir: {url}")
    except Exception:
        pass  # advisory only — never let it disturb the state mutation


def _completion_actuals(rdir: Path) -> dict:
    """The run's actuals for the completion event (issue #99): sessions_used
    (count of `session_start` events), first_session_at (the first one's ts),
    and the completion stamp — computed by the tool process, never typed by
    the model. Fail-soft: an events read problem nulls the event-derived
    fields rather than blocking completion."""
    sessions_used = None
    first_session_at = None
    try:
        events = run_state.read_events(rdir, last_n=0)
        sessions_used = run_state.count_session_starts(events)
        first_session_at = next(
            (e.get("ts") for e in events if e.get("type") == "session_start"), None
        )
    except Exception:
        pass
    return {
        "sessions_used": sessions_used,
        "first_session_at": first_session_at,
        "completed_at": run_state.utc_now_iso(),
    }


def cmd_state(args) -> int:
    """Execute state / state set commands."""
    if args.action != "set":
        rdir = run_state.run_dir()
        if rdir is None:
            print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
            return 0
        try:
            state = run_state.load_state(rdir)
        except (run_state.RunStateError, OSError) as exc:
            print(f"⚠️  {exc}", file=sys.stderr)
            return 1
        if state is None:
            print(f"No run state yet at {rdir}")
            return 0
        print(yaml.safe_dump(state, sort_keys=False), end="")
        return 0

    # --- mutation path ---
    if not any([args.phase, args.stop_reason, args.status, args.critical_error, args.question]):
        print("❌ state set requires at least one of --phase/--stop-reason/--status/--question", file=sys.stderr)
        return 1
    if args.stop_reason == "critical_error" and not args.critical_error:
        print("❌ --stop-reason=critical_error requires --critical-error JSON", file=sys.stderr)
        return 1
    if args.question and args.stop_reason not in (None, "question"):
        print("❌ --question is only valid with --stop-reason=question", file=sys.stderr)
        return 1
    critical_error = None
    if args.critical_error:
        try:
            critical_error = json.loads(args.critical_error)
        except json.JSONDecodeError as exc:
            print(f"❌ --critical-error is not valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(critical_error, dict):
            print("❌ --critical-error must be a JSON object, e.g. '{\"summary\": \"...\", \"detail\": \"...\"}'", file=sys.stderr)
            return 1

    rdir, state = _require_run()
    if rdir is None:
        return 1
    # The "after" form (issue #97): a bare `--question` is only valid while
    # the recorded stop reason is already `question`.
    if args.question and not args.stop_reason and state.get("stop_reason") != "question":
        print("❌ --question requires --stop-reason=question (pass both, or set it first)", file=sys.stderr)
        return 1

    changed = {}
    phase_changed = False
    old_phase = state.get("phase")  # the step the transition ends (issue #100)
    if args.phase and args.phase != state.get("phase"):
        state["phase"] = args.phase
        changed["phase"] = args.phase
        phase_changed = True
    if args.stop_reason:
        state["stop_reason"] = None if args.stop_reason == "none" else args.stop_reason
        changed["stop_reason"] = state["stop_reason"]
        if state["stop_reason"] != "critical_error":
            state["critical_error"] = None
        # A stale question must not survive ANY new stop — even a fresh
        # question-stop starts blank; `--question` below re-sets the text.
        state["open_question"] = None
    if args.question:
        # Agent-typed free text landing in the (shared) work repo — redact
        # like the other writers (log, ledger title/note, artifacts) do.
        state["open_question"] = redact_secrets(args.question)
        changed["open_question"] = state["open_question"]
    if critical_error is not None:
        state["critical_error"] = critical_error
        changed["critical_error"] = True
    if args.status:
        state["status"] = args.status
        changed["status"] = args.status

    if not changed:
        print("✅ State unchanged")
        return 0

    # Pre-execution freeze gate (issue #87 D2): the first phase transition
    # out of the planning family finalizes the run's identity — the frozen
    # stamp lands in this same state write, and a named run's dir takes its
    # single name-bearing rename. Fail-soft: a freeze problem must not
    # block the state mutation itself.
    old_dir_name = None
    if (
        phase_changed
        and not run_state.is_planning_phase(args.phase)
        and not state.get("frozen")
    ):
        try:
            rdir, old_dir_name = run_state.freeze_run_dir(rdir, state)
        except Exception as exc:
            print(f"⚠️  pre-execution freeze skipped: {exc}")

    run_state.write_state(rdir, state)
    if phase_changed:
        run_state.append_event(rdir, "phase", note=args.phase)
    if set(changed) - {"phase"}:
        event_data = dict(changed)
        if args.status == "complete":
            # Estimate-vs-actual raw data (issue #99): the completion event
            # carries the actuals — no new state field needed.
            event_data["actuals"] = _completion_actuals(rdir)
        run_state.append_event(rdir, "state_changed", data=event_data)
    print(f"✅ State updated: {changed}")

    # Durability (spec §4.4): push on phase transitions and completion.
    if phase_changed or args.status == "complete":
        detail = f"phase={args.phase}" if phase_changed else f"status={args.status}"
        _push_run_dir(state, detail, old_dir_name)
    # Pushed-deliverable advisory (issue #100): checked AFTER the durability
    # push, so it fires only when that push left the previous step's
    # artifacts unpushed. A first phase set (no old phase) ends no step.
    if phase_changed and old_phase:
        _advise_unpushed_phase_end(rdir, old_phase, args.phase)
    return 0


def cmd_answer(text: str) -> int:
    """Execute answer command (issue #98 — resume-on-answer).

    Applies a human's answer to the run's recorded open question through
    run_state.answer_question (appends `question_answered`, clears
    `open_question` + `stop_reason`; status untouched), then pushes the run
    dir — the answer is exactly the record a fresh session resumes from, so
    it must not stay local. Mutating-verb rules: no run context and no open
    question recorded are errors (exit 1). No auto-seed: an answer can only
    land on a run that already asked something.
    """
    if not text.strip():
        print("❌ answer requires a non-empty text", file=sys.stderr)
        return 1
    rdir = run_state.run_dir()
    if rdir is None:
        print("❌ No run context: LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1
    try:
        state = run_state.load_state(rdir)
    except (run_state.RunStateError, OSError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    if state is None or not state.get("open_question"):
        print("❌ No open question recorded — nothing to answer", file=sys.stderr)
        return 1
    try:
        run_state.answer_question(rdir, state, text)
    except (run_state.RunStateError, OSError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print("✅ Question answered — open question and question stop cleared")
    _push_run_dir(state, "question answered", saved="answer")
    return 0


def _last_non_empty_line(tail: bytes) -> str | None:
    """Best-effort final line of captured output; None when there is none."""
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if line:
            return line
    return None


def cmd_verify(name: str, command: list[str]) -> int:
    """Execute verify command (issue #88 D2 — receipts for non-gate validation).

    Runs the command with stderr merged into stdout (so the hashed tail sees
    the whole story, like `2>&1`), streams the output through to stdout,
    mirrors the command's exit code, and appends a `verify` receipt event —
    written by this tool process, never typed by the model. The caller
    (main) has already enforced and stripped the `--` separator; `command`
    is the raw post-separator argv. Mutating-verb rules apply: without run
    context this errors out BEFORE running the command (a validation whose
    receipt can never land proves nothing).
    A receipt-append failure after the command ran is reported loudly but
    the exit code still mirrors the command — the run's result stays
    truthful for pipelines either way.
    """
    command = list(command or [])
    if not command:
        print("❌ verify requires a command after `--`", file=sys.stderr)
        return 1
    name = name.strip()
    if not name:
        print("❌ verify requires a non-empty receipt name", file=sys.stderr)
        return 1

    rdir, _state = _require_run()
    if rdir is None:
        return 1

    started = time.monotonic()
    tail = bytearray()
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
    except (OSError, ValueError) as exc:
        # Shell convention: 127 for a command that could not be started.
        print(f"❌ verify could not start {command[0]!r}: {exc}", file=sys.stderr)
        exit_code = 127
    else:
        echo = sys.stdout.buffer
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                if echo is not None:
                    try:
                        echo.write(chunk)
                        echo.flush()
                    except BrokenPipeError:
                        # Downstream consumer closed early (`… | head`):
                        # stop echoing but keep draining and hashing so the
                        # receipt still lands and the exit code still
                        # mirrors the command.
                        echo = None
                tail += chunk
                if len(tail) > VERIFY_TAIL_BYTES:
                    del tail[: len(tail) - VERIFY_TAIL_BYTES]
            exit_code = proc.wait()
        except KeyboardInterrupt:
            # The child shares the terminal's SIGINT; reap it and mirror
            # the shell convention for an interrupted command.
            proc.wait()
            exit_code = 130
        if exit_code < 0:
            # Signal-killed: wait() reports -N; mirror the shell's 128+N so
            # the receipt and the observed exit code agree.
            exit_code = 128 - exit_code

    data = {
        "name": name,
        "argv": [redact_secrets(arg) for arg in command],
        "exit_code": exit_code,
        "duration_s": round(time.monotonic() - started, 1),
        "output_tail_sha256": hashlib.sha256(bytes(tail)).hexdigest(),
    }
    summary_line = _last_non_empty_line(bytes(tail))
    if summary_line is not None:
        data["summary_line"] = redact_secrets(summary_line)
    note = f"{name}: {'pass' if exit_code == 0 else f'exit {exit_code}'}"
    try:
        run_state.append_event(rdir, "verify", note=note, data=data)
        # Receipt chrome goes to stderr: the command's own stdout streams
        # through untouched for pipelines.
        print(f"✅ Verify receipt recorded: {note}", file=sys.stderr)
    except Exception as exc:
        print(
            f"❌ verify receipt NOT recorded ({exc}) — exit code still mirrors the command",
            file=sys.stderr,
        )
    return exit_code


def cmd_event(args) -> int:
    """Execute event command."""
    data = None
    if args.data:
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as exc:
            print(f"❌ --data is not valid JSON: {exc}", file=sys.stderr)
            return 1
    rdir, _state = _require_run()
    if rdir is None:
        return 1
    run_state.append_event(rdir, args.type, note=args.note, data=data)
    print(f"✅ Event appended: {args.type}")
    return 0


def _normalize_name(value: str) -> str:
    """Kebab-case normalization (spec §1): lowercase; spaces/underscores to
    '-'; strip anything outside [a-z0-9-]; collapse '-' runs; trim ends."""
    value = re.sub(r"[ _]", "-", value.lower())
    value = re.sub(r"[^a-z0-9-]", "", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def _validate_name(value: str) -> str | None:
    """Normalize and validate a run name for the mutating verbs.

    Shared by `work name` and `work seed --name` so the naming rules can't
    drift between them. Prints the normalization note / errors itself;
    returns the kebab-case name, or None when nothing valid survives.
    """
    name = _normalize_name(value)
    if not name:
        print(f"❌ Nothing left of {value!r} after kebab-case normalization", file=sys.stderr)
        return None
    if name != value:
        print(f"Normalized to: {name}")
    if name == "archive":
        # The cleaner's archive/ subtree shares the runs namespace; if the
        # name-as-directory growth path lands, this name would collide.
        print("❌ 'archive' is a reserved name (the archived-runs subtree)", file=sys.stderr)
        return None
    return name


def _name_conflict(rdir: Path, name: str) -> str | None:
    """Slug of another run in this project already holding `name`, or None.

    Scans sibling run dirs for state.yaml (plus legacy state.yml).
    Corrupt/unreadable siblings are skipped — a broken run must not block
    naming — and the archive/ subtree is ignored (archived runs no longer
    hold their names). A sibling's slug also holds its name: a name equal
    to another run's slug would let the name shadow that run's address.
    The slug is checked from the sibling's recorded state (dirs get
    renamed to `<slug>--<name>` at the freeze, so the dir name alone no
    longer carries the slug), with the dir-name check kept as a fallback
    for stateless dirs.
    """
    base = rdir.parent
    if not base.is_dir():
        return None
    for sibling in sorted(base.iterdir()):
        if not sibling.is_dir() or sibling.name in (rdir.name, "archive"):
            continue
        if sibling.name == name:
            return sibling.name
        sib_state = run_state._read_sibling_state(sibling)
        if sib_state is not None and name in (
            sib_state.get("name"), sib_state.get("slug")
        ):
            return sibling.name
    return None


def cmd_name(value: str | None) -> int:
    """Execute name command (spec §1 `work name`)."""
    if value is None:
        # Display path — read-only; never breaks a session (always 0).
        rdir = run_state.run_dir()
        if rdir is None:
            print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
            return 0
        try:
            state = run_state.load_state(rdir)
        except (run_state.RunStateError, OSError) as exc:
            print(f"⚠️  {exc}", file=sys.stderr)
            return 0
        if state is None or not state.get("name"):
            print("No name set")
        else:
            print(state["name"])
        return 0

    # --- mutation path ---
    name = _validate_name(value)
    if name is None:
        return 1
    rdir, state = _require_run()
    if rdir is None:
        return 1
    if state.get("name") == name:
        print(f"✅ Name unchanged: {name}")
        return 0
    holder = _name_conflict(rdir, name)
    if holder is not None:
        print(f"❌ Name '{name}' is already held by run '{holder}' (names are unique per project)", file=sys.stderr)
        return 1
    state["name"] = name
    run_state.write_state(rdir, state)
    run_state.append_event(rdir, "run_named", note=name)
    print(f"✅ Run named: {name}")
    return 0


def cmd_ledger(args) -> int:
    """Execute ledger / ledger set commands (issue #89).

    `work ledger set` is the ONLY writer of the task↔commit mapping — gates
    stay ledger-unaware (spec decision 3). Every mutation lands both the
    ledger.yaml snapshot and a `task` audit event, then pushes: the ledger
    write is exactly the record a dead session must not lose.
    """
    if args.action != "set":
        # Read-only display; no ledger is a normal state (exit 0).
        rdir = run_state.run_dir()
        if rdir is None:
            print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
            return 0
        try:
            ledger = run_state.load_ledger(rdir)
        except (run_state.RunStateError, OSError) as exc:
            print(f"⚠️  {exc}", file=sys.stderr)
            return 1
        print(run_state.format_ledger(ledger))
        return 0

    # --- mutation path ---
    task_id = (args.task_id or "").strip()
    if not task_id:
        print("❌ ledger set requires a task id: work ledger set <task-id> --status <s>", file=sys.stderr)
        return 1
    if not args.status:
        print("❌ ledger set requires --status", file=sys.stderr)
        return 1
    rdir, state = _require_run()
    if rdir is None:
        return 1
    if args.status == "done" and not args.commit:
        # Loud but non-fatal: docs-only tasks legitimately have no commit,
        # but a forgotten sha is exactly what crash recovery later misses.
        print(
            f"⚠️  '{task_id}' marked done with NO --commit — fine for docs-only "
            "tasks; otherwise re-run with --commit <sha> so recovery can find it",
            file=sys.stderr,
        )
    try:
        run_state.set_ledger_task(
            rdir,
            task_id,
            args.status,
            title=args.title,
            commit=args.commit,
            receipt=args.receipt,
            note=args.note,
        )
    except (run_state.RunStateError, OSError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(f"✅ Ledger updated: {task_id} = {args.status}")

    # Durability: the ledger row is the crash-recovery record — push now,
    # not at session end (non-fatal, like artifact writes).
    rc = commit_work_path(
        run_state.run_rel_path_candidates(),
        f"run-state: {state['slug']} ledger {task_id}={args.status}",
    )
    if rc != 0:
        print("⚠️  Warning: run-state push failed (ledger saved locally)")
    return 0


def cmd_plan_check() -> int:
    """Execute `work plan check` (issue #90 — checkable plan gates).

    Read-only by contract: reads plan.index.json (+ plan.md / goals.md when
    present) from the run dir, feeds the pure lint kernel, prints the
    findings report to stdout. Exit 1 only when the lint finds errors —
    no run context and no plan index are both clean exits (chat/review
    taskdefs have no index and must not be nagged). Writes nothing: no
    event append, no push — safe to run anywhere, any number of times.
    """
    rdir = run_state.run_dir()
    if rdir is None:
        print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
        return 0
    index_path = rdir / plan_index.PLAN_INDEX_FILE
    if not index_path.is_file():
        print(f"No plan index ({index_path} not found) — nothing to check")
        return 0
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"❌ cannot read {index_path}: {exc}", file=sys.stderr)
        return 1

    index, findings = plan_index.parse_plan_index(index_text)
    if index is not None:
        # Sibling documents are optional context for the drift/goal rules;
        # an unreadable sibling degrades to "absent" rather than failing a
        # read-only lint.
        def _sibling(name: str) -> str | None:
            path = rdir / name
            try:
                return path.read_text(encoding="utf-8") if path.is_file() else None
            except OSError:
                return None

        findings = plan_index.lint_plan_index(
            index, plan_md=_sibling("plan.md"), goals_md=_sibling("goals.md")
        )

    task_count = len(index.get("tasks") or []) if isinstance(index, dict) else 0
    print(f"Plan check: {index_path} — {task_count} task(s)")
    for line in plan_index.format_findings(findings):
        print(line)
    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]
    if errors:
        print(f"❌ plan check failed: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    if warnings:
        print(f"✅ plan check green ({len(warnings)} warning(s) above are non-blocking)")
    else:
        print("✅ plan check green: DAG acyclic, write-scopes disjoint, session scopes declared")
    return 0


def _read_goals_md(rdir: Path) -> str | None:
    """Text of the run's goals.md, or None when absent/unreadable.

    Redacted at read: goal statements, evidence, and topic seed all flow
    into events.jsonl (goals_frozen/goal_amended payloads, amend diffs), so
    like every other agent-typed text landing in the shared work repo they
    must never carry a secret. Redacting the source text ONCE keeps every
    derived value — canonical goals, diffs, and crucially the hash — computed
    over the same bytes, so hash comparisons across verbs stay stable
    (redaction is deterministic, and `work artifact` already redacts the
    file itself on copy-in).
    """
    path = rdir / goals.GOALS_FILE
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    except OSError:
        return None
    return redact_secrets(text) if text is not None else None


def _print_goal_findings(findings) -> bool:
    """Print lint findings; True when any is an error (blocks the verb)."""
    for line in goals.format_findings(findings):
        print(line)
    return any(f.level == "error" for f in findings)


def _receipt_names(events: list[dict]) -> set[str]:
    """Names of recorded verify/gate receipts, for evidence classification."""
    names: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event.get("type") == "verify" and data.get("name"):
            names.add(str(data["name"]))
        elif event.get("type") == "gate" and data.get("gate"):
            names.add(str(data["gate"]))
    return names


def _cmd_goals_status() -> int:
    """Bare `work goals`: read-only status display. Always exits 0."""
    rdir = run_state.run_dir()
    if rdir is None:
        print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
        return 0
    text = _read_goals_md(rdir)
    if text is None:
        print(f"No goals.md in run dir ({rdir})")
        return 0
    parsed = goals.parse_goals(text)
    active = goals.active_goals(parsed)
    tombstoned = len(parsed["goals"]) - len(active)
    current_hash = goals.goals_hash(parsed)
    print(f"Goals: {len(active)} active, {tombstoned} tombstoned ({current_hash})")
    last = goals.latest_goals_event(run_state.read_events(rdir, last_n=0))
    if last is None:
        print("Not frozen — `work goals freeze` records the agreed set at spec approval")
    elif last["goals_hash"] == current_hash:
        print("Frozen — goals.md matches the last frozen/amended set")
    else:
        print(f"⚠️  DIVERGED from the last frozen/amended set ({last['goals_hash']}) "
              "— run `work goals amend`")
    return 0


def _cmd_goals_check() -> int:
    """`work goals check`: read-only draft lint (spec D2). The freeze
    contract (signal class, evidence) reports as warnings here — drafts may
    sketch goals before naming their proof — and structural problems error."""
    rdir = run_state.run_dir()
    if rdir is None:
        print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
        return 0
    text = _read_goals_md(rdir)
    if text is None:
        print(f"No goals.md ({rdir / goals.GOALS_FILE} not found) — nothing to check")
        return 0
    parsed = goals.parse_goals(text)
    findings = goals.validate_goals(parsed)
    print(f"Goals check: {rdir / goals.GOALS_FILE} — "
          f"{len(goals.active_goals(parsed))} active goal(s)")
    has_errors = _print_goal_findings(findings)
    warnings = [f for f in findings if f.level == "warning"]
    if has_errors:
        errors = [f for f in findings if f.level == "error"]
        print(f"❌ goals check failed: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    if warnings:
        print(f"✅ goals check green ({len(warnings)} warning(s) above become "
              "errors at freeze)")
    else:
        print("✅ goals check green: every goal names its signal and evidence")
    return 0


def _cmd_goals_freeze(note: str | None) -> int:
    """`work goals freeze`: the spec-approval gate (spec D2 + decision 1).

    Strict-validates goals.md (signal enum + evidence required), records the
    `goals_frozen` event carrying the canonical goal list + hash — the
    agreed set every later amend/assess is measured against — registers
    goals.md in state.artifacts, and invokes the run's pre-execution freeze
    seam (`freeze_run_dir`: `frozen` stamp + one-shot name-bearing dir
    rename) when the run is named and not already frozen: both mark the
    same gate. An UNNAMED run's seam is left to the phase gate instead —
    the frozen stamp would forfeit the single rename forever, and spec
    approval can precede the run being named. Re-freezing is an error —
    post-freeze changes go through amend.
    """
    rdir, state = _require_run()
    if rdir is None:
        return 1
    text = _read_goals_md(rdir)
    if text is None:
        print(f"❌ No goals.md in the run dir ({rdir}) — draft it during "
              "spec/brainstorm, then freeze at approval", file=sys.stderr)
        return 1
    if goals.latest_goals_event(run_state.read_events(rdir, last_n=0)) is not None:
        print("❌ Goals are already frozen — use `work goals amend` for changes",
              file=sys.stderr)
        return 1
    parsed = goals.parse_goals(text)
    if _print_goal_findings(goals.validate_goals(parsed, strict=True)):
        print("❌ Cannot freeze: fix the errors above (every active goal must "
              "name its signal class and evidence source)", file=sys.stderr)
        return 1

    old_dir_name = None
    if not state.get("frozen"):
        if not state.get("name"):
            # A run can still be unnamed at spec approval, and the
            # frozen stamp would forfeit the one-shot name-bearing rename
            # forever ("no second chance") — leave the seam to the phase
            # gate, which fires at the first execution-family transition.
            print("⚠️  run not named yet — pre-execution freeze left to the "
                  "phase gate (name the run with `work name`)")
        else:
            # Fail-soft like cmd_state's phase-transition freeze: a rename
            # problem must not block recording the agreed goal set.
            try:
                rdir, old_dir_name = run_state.freeze_run_dir(rdir, state)
            except Exception as exc:
                print(f"⚠️  pre-execution freeze skipped: {exc}")
    state.setdefault("artifacts", {})["goals"] = goals.GOALS_FILE
    run_state.write_state(rdir, state)
    goals_hash_value = goals.goals_hash(parsed)
    run_state.append_event(
        rdir, goals.GOALS_FROZEN_EVENT,
        note=redact_secrets(note) if note else None,
        data={
            "goals_hash": goals_hash_value,
            "topic_seed": parsed["topic_seed"],
            "goals": goals.canonical_goals(parsed),
        },
    )
    print(f"✅ Goals frozen: {goals_hash_value} "
          f"({len(goals.active_goals(parsed))} active goal(s))")
    _push_run_dir(state, "goals frozen", old_dir_name, saved="goals change")
    return 0


def _cmd_goals_amend(note: str | None) -> int:
    """`work goals amend`: explicit post-freeze change (spec D2). Validates
    the edited goals.md against the last agreed set with the
    tombstone-not-renumber rules and records the `goal_amended` event with
    the diff — a goals.md edit without this is exactly the silent
    divergence assess reports."""
    rdir, state = _require_run()
    if rdir is None:
        return 1
    text = _read_goals_md(rdir)
    if text is None:
        print(f"❌ No goals.md in the run dir ({rdir})", file=sys.stderr)
        return 1
    last = goals.latest_goals_event(run_state.read_events(rdir, last_n=0))
    if last is None:
        print("❌ Goals are not frozen yet — use `work goals freeze`", file=sys.stderr)
        return 1
    parsed = goals.parse_goals(text)
    new_hash = goals.goals_hash(parsed)
    if new_hash == last["goals_hash"]:
        print("✅ No changes to amend — goals.md matches the frozen set")
        return 0
    if _print_goal_findings(goals.validate_amendment(last["goals"], parsed["goals"])):
        print("❌ Cannot amend: fix the errors above (removed goals tombstone, "
              "ids never renumber)", file=sys.stderr)
        return 1
    diff = goals.amendment_diff(last["goals"], parsed["goals"])
    run_state.append_event(
        rdir, goals.GOAL_AMENDED_EVENT,
        note=redact_secrets(note) if note else None,
        data={
            "old_goals_hash": last["goals_hash"],
            "new_goals_hash": new_hash,
            "diff": diff,
            "topic_seed": parsed["topic_seed"],
            "goals": goals.canonical_goals(parsed),
        },
    )
    for change in diff:
        print(f"  {change['id']}: {change['change']}")
    if diff:
        print(f"✅ Goals amended: {new_hash} ({len(diff)} change(s))")
    else:
        # The hash moved but no goal changed — a topic-seed edit (the only
        # per-goal-invisible part of the canonical identity).
        print(f"✅ Goals amended: {new_hash} (topic-seed change)")
    _push_run_dir(state, "goals amended", saved="goals change")
    return 0


def _cmd_goals_assess(verdict_flags: list[str], note: str | None) -> int:
    """`work goals assess`: the finish gate (spec D2 + D3, nudge-don't-block).

    Bare: prints the per-goal verdict skeleton for the session to complete
    into retro.md, plus a divergence report — read-only and never a
    failure once a run context exists (missing/invalid goals.md degrades
    to a note over the skeleton path). With repeatable
    `--verdict 'G<N>=<verdict>:<evidence>'` flags: validates a complete
    verdict map over every active goal, classifies each evidence string
    against recorded receipts and registered artifacts (free prose is
    allowed but marked), records the `goals_assessed` event, and prints the
    completed table for retro.md. Divergence from the last frozen/amended
    hash is reported and recorded, never blocking.
    """
    recording = bool(verdict_flags)
    state: dict | None = None
    if recording:
        rdir, state = _require_run()
        if rdir is None:
            return 1
    else:
        rdir = run_state.run_dir()
        if rdir is None:
            print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
            return 0
    text = _read_goals_md(rdir)
    if text is None:
        if recording:
            print(f"❌ No goals.md in the run dir ({rdir}) — nothing to assess",
                  file=sys.stderr)
            return 1
        # Bare form is the nudge path — a run without goals gets a note,
        # never a failure (check-verb symmetry).
        print(f"No goals.md ({rdir / goals.GOALS_FILE} not found) — nothing to assess")
        return 0
    parsed = goals.parse_goals(text)
    if _print_goal_findings(goals.validate_goals(parsed, strict=True)):
        if recording:
            print("❌ Cannot record against a goal set that fails strict "
                  "validation — fix goals.md (and `work goals amend`) first",
                  file=sys.stderr)
            return 1
        # Bare form: report, then still print the skeleton (nudge-don't-block).
        print("⚠️  goals.md fails strict validation (see above) — fix it "
              "before recording verdicts")

    events = run_state.read_events(rdir, last_n=0)
    last = goals.latest_goals_event(events)
    current_hash = goals.goals_hash(parsed)
    diverged = last is not None and last["goals_hash"] != current_hash
    if last is None:
        print("⚠️  Goals were never frozen — assessing the working goals.md")
    elif diverged:
        print(f"⚠️  goals.md has DIVERGED from the last frozen/amended set "
              f"({last['goals_hash']}) — a silent edit; `work goals amend` "
              "it or explain in the retro")

    if not recording:
        print()
        print(goals.render_verdict_skeleton(parsed["goals"]))
        receipts = sorted(_receipt_names(events))
        if receipts:
            print()
            print(f"Receipts available to cite as evidence: {', '.join(receipts)}")
        print("\nRecord with: work goals assess --verdict 'G1=met:<evidence>' …")
        return 0

    verdicts: dict[str, dict] = {}
    for flag in verdict_flags:
        try:
            goal_id, verdict, evidence = goals.parse_verdict_flag(flag)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        if goal_id in verdicts:
            print(f"❌ Duplicate --verdict for goal {goal_id}", file=sys.stderr)
            return 1
        verdicts[goal_id] = {"verdict": verdict, "evidence": redact_secrets(evidence)}
    if _print_goal_findings(goals.validate_verdicts(parsed["goals"], verdicts)):
        print("❌ Cannot record: the assessment must cover every active goal, "
              "exactly", file=sys.stderr)
        return 1

    artifact_names: set[str] = set()
    if isinstance(state, dict):
        artifacts = state.get("artifacts") or {}
        artifact_names = {str(v) for v in artifacts.values()} | {str(k) for k in artifacts}
    receipt_names = _receipt_names(events)
    for entry in verdicts.values():
        entry["evidence_kind"] = goals.classify_evidence(
            entry["evidence"], receipt_names, artifact_names)

    counts = {v: 0 for v in goals.GOAL_VERDICTS}
    for entry in verdicts.values():
        counts[entry["verdict"]] += 1
    summary = ", ".join(f"{v} {counts[v]}" for v in goals.GOAL_VERDICTS if counts[v])
    data = {"goals_hash": current_hash, "verdicts": verdicts, "diverged": diverged}
    if last is not None:
        data["last_agreed_hash"] = last["goals_hash"]
    run_state.append_event(
        rdir, goals.GOALS_ASSESSED_EVENT,
        note=redact_secrets(note) if note else summary + (" (diverged)" if diverged else ""),
        data=data,
    )
    print()
    print(goals.render_verdict_table(parsed["goals"], verdicts))
    print()
    print(f"✅ Goals assessed: {summary} — land the table above in retro.md")
    _push_run_dir(state, "goals assessed", saved="goals change")
    return 0


def cmd_goals(args) -> int:
    """Execute goals verbs (issue #91 — frozen goal-sets)."""
    if args.verdict and args.goals_action != "assess":
        print("❌ --verdict is only valid with `work goals assess`", file=sys.stderr)
        return 1
    if args.note and args.goals_action in (None, "check"):
        print("❌ --note is only valid with freeze/amend/assess", file=sys.stderr)
        return 1
    if args.goals_action == "check":
        return _cmd_goals_check()
    if args.goals_action == "freeze":
        return _cmd_goals_freeze(args.note)
    if args.goals_action == "amend":
        return _cmd_goals_amend(args.note)
    if args.goals_action == "assess":
        return _cmd_goals_assess(args.verdict, args.note)
    return _cmd_goals_status()


def cmd_resume(as_json: bool = False) -> int:
    """Execute resume command. Read-only; never breaks a session (always 0)."""
    rdir = run_state.run_dir()
    if rdir is None:
        print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
        return 0
    try:
        state = run_state.load_state(rdir)
        # All events, not just the brief's tail: the sessions-used count
        # (issue #99) needs the whole log; the brief still shows the last 5.
        all_events = run_state.read_events(rdir, last_n=0)
    except (run_state.RunStateError, OSError) as exc:
        print(f"⚠️  Run state unreadable — falling back to worklog. ({exc})")
        return 0
    try:
        ledger = run_state.load_ledger(rdir)
    except (run_state.RunStateError, OSError) as exc:
        # A broken ledger must not break resume — the brief just omits it.
        ledger = None
        print(f"⚠️  ledger unreadable — brief omits it. ({exc})", file=sys.stderr)
    decision = run_state.decide(
        state, all_events[-5:], run_state.current_session_id(), ledger=ledger,
        sessions_used=run_state.count_session_starts(all_events),
    )
    if decision.get("kind") == "run":
        # Dirs are renamed by the lifecycle (issue #87 D2), so consumers —
        # e.g. the stop-hook guard's push check — need the resolved path,
        # not a slug-derived guess.
        decision["run_dir"] = str(rdir)
        # The same consumers' human-facing half (issue #100): the guard's
        # push nudge cites this clickable link instead of a container path.
        # Fail-soft — no derivable URL simply omits the field.
        run_dir_url = web_url_for(rdir)
        if run_dir_url:
            decision["run_dir_url"] = run_dir_url
    if as_json:
        print(json.dumps(decision, ensure_ascii=False))
    else:
        print(run_state.format_brief(
            decision,
            seed=os.environ.get("LMER_START_PROMPT") or None,
            # Web (tree) URL of the run dir (issue #104) — fail-soft None
            # keeps the brief byte-identical when no URL is derivable.
            run_dir_url=web_url_for(rdir),
        ))
    return 0


def _materialize_artifact(source: Path, dest: Path, rdir: Path) -> str | None:
    """Materialize `source` at `dest` (link, copy+redact, or redact in place); return the relative link target when linked, else None."""
    # Single canonical home (issue #103): a source that already lives inside
    # the RUN DIR is the canonical file — the registered name becomes a
    # RELATIVE symlink to it (exactly like the masterplan run-root links),
    # never a second copy that can drift. No redaction pass on the link
    # branch: everything under the run dir is pushed verbatim by this
    # command's own durability push (`git add -A` on the run path), so
    # redacting a copy of a run-dir file never protected anything. A
    # work-repo source OUTSIDE the run dir keeps the copy+redact path: its
    # canonical file is not staged by this command, and a file hand-written
    # elsewhere in the checkout may never have passed through a redacting
    # writer at all — linking it would trade a redacted copy for a raw one.
    rdir_res = rdir.resolve()
    canonical = source.resolve()
    linked: str | None = None  # the relative link target when we link
    if canonical == dest.resolve():
        if dest.is_symlink():
            # Re-registering an existing link (by its own path or by its
            # canonical target): already the canonical-home shape — leave
            # the link exactly as it is.
            linked = os.readlink(dest)
        else:
            # Registering the canonical file in place: rewrite it through
            # redaction, as the copy path always has (it IS the run-dir
            # file that the durability push publishes).
            dest.write_text(
                redact_secrets(source.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
    elif canonical.is_file() and canonical.is_relative_to(rdir_res):
        linked = os.path.relpath(canonical, rdir_res)
        if dest.is_symlink() and os.readlink(dest) == linked:
            pass  # already correct — idempotent re-registration
        else:
            if dest.is_symlink() or dest.exists():
                # Replace an older copy (or re-point a stale link) — the
                # in-run source is the one canonical home.
                dest.unlink()
            dest.symlink_to(linked)
    else:
        # External source (e.g. /tmp scratch — it would vanish with the
        # container) or a work-repo file outside the run dir (see above):
        # copy it in through secret redaction, exactly as before.
        content = redact_secrets(source.read_text(encoding="utf-8"))
        if dest.is_symlink():
            # Never write THROUGH a stale link — that would clobber the
            # canonical file it points at. Replace the link with the copy.
            dest.unlink()
        dest.write_text(content, encoding="utf-8")
    return linked


def cmd_artifact(name: str | None, file_path: str | None, sync: bool = False) -> int:
    """Execute artifact command (spec §4.3 `work artifact`, §6 `--sync`)."""
    if sync:
        # Manual masterplan artifact-link sync (spec §6). Standalone mode:
        # combining it with a copy invocation is ambiguous — reject cleanly.
        if name is not None or file_path is not None:
            print("❌ --sync takes no artifact name or --file", file=sys.stderr)
            return 1
        rdir = run_state.run_dir()
        if rdir is None:
            print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset) — nothing to sync")
            return 0
        linked = _sync_masterplan_links()
        if linked:
            print(f"✅ Masterplan artifacts linked: {', '.join(linked)}")
        else:
            print("No masterplan artifacts to link")
        return 0

    if name is None:
        print("❌ artifact requires a name (or --sync)", file=sys.stderr)
        return 1
    if file_path is None:
        print("❌ artifact requires --file/-f (or --sync)", file=sys.stderr)
        return 1
    if not name or Path(name).name != name or name.startswith("."):
        print(f"❌ Invalid artifact name: {name!r} (plain filename required)", file=sys.stderr)
        return 1
    reserved = (
        run_state.STATE_FILE,
        run_state.LEGACY_STATE_FILE,
        run_state.EVENTS_FILE,
        run_state.LEDGER_FILE,
    )
    reserved_prefixes = tuple(
        f"{f}."
        for f in (run_state.STATE_FILE, run_state.LEGACY_STATE_FILE, run_state.LEDGER_FILE)
    )
    if name in reserved or name.startswith(reserved_prefixes):
        print(f"❌ Reserved artifact name: {name}", file=sys.stderr)
        return 1
    source = Path(file_path)
    if not source.exists():
        print(f"❌ Artifact source not found: {file_path}", file=sys.stderr)
        return 1
    rdir, state = _require_run()
    if rdir is None:
        return 1
    dest = rdir / name
    canonical = source.resolve()
    linked = _materialize_artifact(source, dest, rdir)
    state.setdefault("artifacts", {})[Path(name).stem] = name
    run_state.write_state(rdir, state)
    run_state.append_event(rdir, "artifact_written", note=name)
    if linked is not None:
        print(f"✅ Artifact linked: {dest} → {linked}")
    else:
        print(f"✅ Artifact registered: {dest}")
    url = web_url_for(canonical if linked is not None else dest)
    if url:
        print(f"   Web: {url}")

    # Central specs index (issue #101): a spec-class artifact also gets a
    # dated relative symlink under {host}/{project}/specs/. A linked
    # registration indexes the CANONICAL file, never the run-root link
    # (#103 — the index entry's basename stays the registered name via
    # `alias`). Fail-soft by contract — an index problem never fails the
    # registration.
    spec_link = specs_index.upsert_spec_link(
        canonical if linked is not None else dest,
        specs_index.run_label(rdir, state),
        alias=name,
    )
    if spec_link is not None:
        print(f"   Specs index: {spec_link}")

    # Durability: artifacts are exactly what a dead session must not lose —
    # push the run dir now rather than waiting for session end (non-fatal).
    # Candidates (resolved + bare-slug dirs) so a rename whose own push
    # failed still gets its old path's deletions staged here. The specs
    # index rides along when this registration touched it.
    stage_paths = run_state.run_rel_path_candidates()
    if spec_link is not None and specs_index.specs_rel_path():
        stage_paths.append(specs_index.specs_rel_path())
    rc = commit_work_path(
        stage_paths,
        f"run-state: {state['slug']} artifact {name}",
    )
    if rc != 0:
        print("⚠️  Warning: run-state push failed (artifact saved locally)")
    return 0


def cmd_specs_index(rebuild: bool) -> int:
    """Execute specs-index command (issue #101 — central specs directory).

    Without flags, lists the index's entries (a directory listing IS the
    index — v1 keeps no README/index file beside the symlinks). With
    `--rebuild`, rebuilds the whole index from runs/ — the backfill path
    for specs registered before the index existed. Rebuild writes only;
    the caller batches the push with `work commit` (same discipline as
    `work seed`).
    """
    if specs_index.specs_dir() is None:
        print("❌ No run context: LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1
    if rebuild:
        created = specs_index.rebuild()
        if created:
            print(f"✅ Specs index rebuilt: {len(created)} entries")
            for link in created:
                print(f"   {link.name} -> {os.readlink(link)}")
            print("Run `work commit` to push the rebuilt index.")
        else:
            print("Specs index rebuilt: no specs found under runs/")
        return 0
    entries = specs_index.list_entries()
    if not entries:
        print("Specs index is empty (see `work specs-index --rebuild` to backfill)")
        return 0
    for link in entries:
        print(f"{link.name} -> {os.readlink(link)}")
    return 0


def cmd_seed(args) -> int:
    """Execute seed command (issue #87 D3 — out-of-session run creation).

    Creates a run for a slug OTHER than the current session's, through the
    same create-tmp → write-state → rename lifecycle as session seeding,
    recording CLI-shaped events (run_seeded, then goal_set / run_named as
    applicable). Seeding is not owning: no `owner` claim is made. Writes
    only — the caller batches the push with `work commit`.
    """
    base = run_state.runs_base()
    if base is None:
        print("❌ No run context: LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1

    slug = run_state.derive_slug(args.taskdef, args.target)
    # match_names too: a sibling run NAMED like this slug would make the
    # new run unfindable by name lookups — refuse the ambiguity outright.
    existing = run_state.find_run_dir(slug, match_names=True)
    if existing is not None:
        print(f"❌ Run '{slug}' already exists at {existing}", file=sys.stderr)
        return 1
    if (base / slug).exists():
        print(f"❌ {base / slug} already exists (stateless dir — not seeding over it)", file=sys.stderr)
        return 1

    name = None
    if args.name:
        name = _validate_name(args.name)
        if name is None:
            return 1
        holder = _name_conflict(base / slug, name)
        if holder is not None:
            print(f"❌ Name '{name}' is already held by run '{holder}' (names are unique per project)", file=sys.stderr)
            return 1

    try:
        rdir, state = run_state.seed_run_dir(
            slug,
            args.taskdef,
            args.target,
            note=f"seeded via `work seed` ({args.taskdef}, target: {args.target})",
            adopt_existing=False,  # seeding must never mutate a run it didn't create
        )
    except (run_state.RunStateError, OSError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    if args.goal:
        state["goal"] = args.goal
    if name:
        state["name"] = name
    if args.goal or name:
        run_state.write_state(rdir, state)
        if args.goal:
            run_state.append_event(rdir, "goal_set", note=args.goal)
        if name:
            run_state.append_event(rdir, "run_named", note=name)

    print(f"✅ Run seeded: {rdir}")
    print("   Not pushed — run `work commit` to publish it.")
    return 0


def _answer_marker_path(answer: str) -> Path:
    """Consume-once marker for a pushed LMER_ANSWER (review on !126).

    The env var lives for the whole container, but an answer belongs to the
    one question it was pushed for: if the agent records a NEW question and
    another `work session-start` runs in the same container (followup /
    restart), the stale value must not be silently applied to a question it
    was never given for. A /tmp marker keyed by the answer's hash shares the
    container's lifetime — exactly the env var's — so "applied once in this
    container" is the right scope.
    """
    digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()[:16]
    return Path(ANSWER_MARKER_DIR or "/tmp") / f".lmer-answer-applied-{digest}"


def cmd_session_start() -> int:
    """Seed-if-absent, claim, log, and print the resume brief. Hook-facing:
    ALWAYS exits 0 — a broken state layer must never break session start."""
    rdir = run_state.run_dir()
    if rdir is None:
        print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset) — run state skipped")
        return 0
    try:
        recovered = False
        try:
            state = run_state.load_state(rdir)
        except run_state.RunStateError as exc:
            if run_state._state_path(rdir) is not None:
                # Newer-schema read-only refusal: the file is intact and must
                # NOT be reseeded over (same distinction ensure_run makes) —
                # a schema-1 seed here would silently downgrade a newer
                # build's run. Fail soft like cmd_session_end.
                print(f"⚠️  run-state untouched at session start: {exc}")
                return 0
            # Corrupt file was backed up by load_state; recover with a fresh seed.
            state = None
            recovered = True
        if state is None:
            if rdir.exists():
                # The dir already holds its final name (and possibly other
                # files, e.g. the backed-up corrupt state) — seed in place.
                state = run_state.seed_state(
                    run_state.derive_slug(),
                    os.environ.get("LMER_TASK", "default"),
                    os.environ.get("LMER_TASK_TARGET", ""),
                )
                run_state.write_state(rdir, state)
                run_state.append_event(rdir, "run_seeded")
            else:
                # Fresh run: create through the tmp-dir-then-rename
                # lifecycle (issue #87 D2).
                rdir, state = run_state.seed_run_dir(
                    run_state.derive_slug(),
                    os.environ.get("LMER_TASK", "default"),
                    os.environ.get("LMER_TASK_TARGET", ""),
                )

        # Resume-on-answer (issue #98): a pushed answer (LMER_ANSWER, set by
        # `lmer --answer "<text>"` on the host) to the run's recorded open
        # question is applied BEFORE deciding, so the brief leads with the
        # answered pair instead of the stale question block. Fail-soft: a
        # problem applying it degrades to the plain brief, never a failure.
        answered = None
        answer = (os.environ.get("LMER_ANSWER") or "").strip()
        if (
            answer
            and state.get("stop_reason") == "question"
            and state.get("open_question")
            and not _answer_marker_path(answer).exists()
        ):
            try:
                question = state.get("open_question")
                state = run_state.answer_question(rdir, state, answer)
                answered = {"question": question, "answer": redact_secrets(answer)}
                try:
                    _answer_marker_path(answer).touch()
                except OSError:
                    pass  # marker is best-effort; the answer still applied
            except Exception as exc:
                print(f"⚠️  LMER_ANSWER not applied (continuing): {exc}")

        # Decide BEFORE claiming so a foreign claim surfaces as a warning.
        # All events for the sessions-used count (issue #99) — this session's
        # own session_start lands after the decide, so the count reads
        # "sessions used so far"; the brief still shows the last 5.
        all_events = run_state.read_events(rdir, last_n=0)
        try:
            ledger = run_state.load_ledger(rdir)
        except (run_state.RunStateError, OSError):
            ledger = None
        decision = run_state.decide(
            state, all_events[-5:], run_state.current_session_id(), ledger=ledger,
            sessions_used=run_state.count_session_starts(all_events),
        )

        run_state.append_event(rdir, "session_start")
        state["owner"] = {
            "session_id": run_state.current_session_id(),
            "claimed_at": run_state.utc_now_iso(),
        }
        run_state.write_state(rdir, state)

        if recovered:
            print("⚠️  Previous state.yaml was unreadable (backed up); recovered with a fresh seed.")
        print(run_state.format_brief(
            decision,
            seed=os.environ.get("LMER_START_PROMPT") or None,
            answered=answered,
            run_dir_url=web_url_for(rdir),
        ))
    except Exception as exc:  # never break a session (spec §6)
        print(f"⚠️  run-state session-start failed (continuing): {exc}")
    return 0


def cmd_session_end() -> int:
    """Record session end and release our claim. Hook-facing: ALWAYS exits 0."""
    rdir = run_state.run_dir()
    if rdir is None:
        return 0
    # Surface masterplan artifacts at the run-dir root before the final
    # staging/push (spec §6). Runs before load_state so any registration the
    # sync writes lands in the state loaded below; fail-soft like the rest.
    _sync_masterplan_links()
    try:
        try:
            state = run_state.load_state(rdir)
        except run_state.RunStateError as exc:
            print(f"⚠️  run-state unreadable at session end: {exc}")
            return 0
        if state is None:
            return 0
        run_state.append_event(rdir, "session_end")
        owner = state.get("owner")
        if isinstance(owner, dict) and owner.get("session_id") == run_state.current_session_id():
            state["owner"] = None
        run_state.write_state(rdir, state)
        rels = run_state.run_rel_path_candidates()
        # Masterplan-sync (and freeze-rename) specs-index entries ride along —
        # this is the last push of the session, so leaving them unstaged
        # strands them locally (review on !126).
        specs_rel = specs_index.specs_rel_path()
        if specs_rel and specs_rel not in rels:
            rels.append(specs_rel)
        rc = commit_work_path(rels, f"run-state: session end {state['slug']}")
        if rc != 0:
            print("⚠️  Warning: run-state push failed at session end (state saved locally)")
    except Exception as exc:
        print(f"⚠️  run-state session-end failed (continuing): {exc}")
    return 0


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
        return cmd_goal(args.description, args.estimate_sessions, args.estimate_time)
    elif args.command == "memory":
        return cmd_memory(args.memory_action, getattr(args, "message", None), parser)
    elif args.command == "setup-workspace":
        return cmd_setup_workspace(args.target, args.task, not args.no_sync)
    elif args.command == "state":
        return cmd_state(args)
    elif args.command == "verify":
        # The `--` separator is REQUIRED, not decorative: without it,
        # `work verify -- pytest tests/` (name forgotten) silently parses
        # as name="pytest", command=["tests/"] and records a receipt named
        # pytest for a command that never was. argparse consumes the first
        # `--` itself, so the contract is enforced on the RAW argv.
        raw = sys.argv[1:]
        if len(raw) < 3 or raw[2] != "--":
            print(
                "❌ verify requires the `--` separator: work verify <name> -- <command …>",
                file=sys.stderr,
            )
            return 1
        return cmd_verify(raw[1], raw[3:])
    elif args.command == "event":
        return cmd_event(args)
    elif args.command == "answer":
        return cmd_answer(args.text)
    elif args.command == "name":
        return cmd_name(args.value)
    elif args.command == "ledger":
        return cmd_ledger(args)
    elif args.command == "goals":
        return cmd_goals(args)
    elif args.command == "plan":
        if args.plan_action == "check":
            return cmd_plan_check()
        parser.print_help()
        return 1
    elif args.command == "resume":
        return cmd_resume(args.as_json)
    elif args.command == "artifact":
        return cmd_artifact(args.name, args.file, args.sync)
    elif args.command == "specs-index":
        return cmd_specs_index(args.rebuild)
    elif args.command == "seed":
        return cmd_seed(args)
    elif args.command == "session-start":
        return cmd_session_start()
    elif args.command == "session-end":
        return cmd_session_end()
    else:
        print(f"❌ Unknown command: {args.command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
