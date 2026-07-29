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
    CLAIM_PUSH_ERROR,
    CLAIM_PUSH_LOST_RACE,
    CLAIM_PUSH_WON,
    claim_push_once,
    commit_napkin_if_subdir,
    commit_work_changes,
    commit_work_path,
    push_napkin_if_separate,
    report_uncommitted_work_items,
    run_dir_push_status,
    run_git_command,
    stageable_paths,
    web_url_for,
)
from . import goals, plan_index, release_run, run_state, specs_index
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


# Bounded attempts for the claim-by-push CAS loop (RUN-STATE.md §7 step 5):
# a non-fast-forward rejection means the remote advanced between fetch and
# push — NOT automatically a lost race (the work repo has many unrelated
# writers) — so re-fetch and re-evaluate, a few times, then fail closed.
RELEASE_CLAIM_ATTEMPTS = 3


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

    # release command (single-flight release claim — RUN-STATE.md §7).
    # Verb names and flags are FROZEN verbatim (§7 R5): taskdef bodies
    # reference them by these exact names.
    release_parser = subparsers.add_parser(
        "release",
        help="Release-run verbs: the single-flight release claim (RUN-STATE.md §7)",
    )
    release_subparsers = release_parser.add_subparsers(
        dest="release_action", help="Release action to perform"
    )
    release_claim_parser = release_subparsers.add_parser(
        "claim",
        help="Take the single-flight release claim (claim-by-push CAS; "
             "non-zero exit = lost or could not establish — fail closed)",
    )
    release_claim_parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the outcome (won / lost pointer / fail-closed) as JSON",
    )
    release_claim_status_parser = release_subparsers.add_parser(
        "claim-status",
        help="Show the release claim: holder, claimed_at, age, live/stale "
             "verdict — or unclaimed (read-only, always exit 0)",
    )
    release_claim_status_parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the claim verdict as JSON",
    )
    release_unclaim_parser = release_subparsers.add_parser(
        "unclaim",
        help="Release our claim (CAS-pushed); a foreign claim refuses "
             "without --force; no claim recorded is a no-op success",
    )
    release_unclaim_parser.add_argument(
        "--force", action="store_true",
        help="Release a FOREIGN claim (human runbook: abort path, "
             "stranded-claim cleanup)",
    )
    release_abort_parser = release_subparsers.add_parser(
        "abort",
        help="Explicitly abort the release run (human declined the release "
             "MR): terminal stop + claim cleared in one state write, "
             "release record marked terminal, CAS-pushed",
    )
    release_abort_parser.add_argument(
        "--reason",
        help="Why the release was declined (free text; redacted before landing)",
    )
    release_abort_parser.add_argument(
        "--force",
        action="store_true",
        help="Abort even while a LIVE foreign claim holds the run (another "
             "session is mid-release); without it such a run refuses, same "
             "as `release unclaim`",
    )
    release_record_parser = release_subparsers.add_parser(
        "record",
        help="Record a release-run fact into release.yaml (single writer; "
             "identity fields write-once, receipts re-recordable)",
    )
    record_subparsers = release_record_parser.add_subparsers(
        dest="record_field", help="Release fact to record"
    )
    record_version_parser = record_subparsers.add_parser(
        "version",
        help="Record leg 1's release version (pyproject version, no 'v' "
             "prefix — the tag name adds it; write-once)",
    )
    record_version_parser.add_argument(
        "value", metavar="X.Y.Z", help="Release version, e.g. 0.5.0"
    )
    record_bump_parser = record_subparsers.add_parser(
        "bump-sha",
        help="Record the bump-MR merge SHA — leg 1 complete "
             "(full 40-hex; write-once)",
    )
    record_bump_parser.add_argument(
        "value", metavar="sha", help="Full 40-hex bump-MR merge SHA"
    )
    record_merge_parser = record_subparsers.add_parser(
        "merge-sha",
        help="Record the release-MR merge SHA every leg-2 step keys on; "
             "hard stop when --version disagrees with leg 1's record",
    )
    record_merge_parser.add_argument(
        "value", metavar="sha", help="Full 40-hex release-MR merge SHA"
    )
    record_merge_parser.add_argument(
        "--version", required=True, dest="observed_version",
        help="Version read from pyproject.toml AT that SHA "
             "(re-proved on every record, even an idempotent one)",
    )
    record_tag_parser = record_subparsers.add_parser(
        "tag",
        help="Record the signed-tag creation receipt; hard stops on "
             "name/SHA drift — never re-point, never re-sign",
    )
    record_tag_parser.add_argument(
        "value", metavar="vX.Y.Z", help="Tag name (exactly v<recorded version>)"
    )
    record_tag_parser.add_argument(
        "--sha", required=True,
        help="The tagged commit (must equal the recorded merge SHA)",
    )
    record_receipt_parser = record_subparsers.add_parser(
        "receipt",
        help="Record a push/upload receipt: github-main-push, "
             "github-tag-push, actions-run, pypi, gitlab-tag-push; "
             "re-recordable (prior values stay in events.jsonl)",
    )
    record_receipt_parser.add_argument(
        "value", metavar="name", help="Receipt name (leg-2 ladder order)"
    )
    record_receipt_parser.add_argument(
        "--url",
        help="The run/URL that actually uploaded "
             "(REQUIRED for actions-run and pypi)",
    )
    record_receipt_parser.add_argument(
        "--note", help="Free-text note (redacted before landing)"
    )
    release_status_parser = release_subparsers.add_parser(
        "status",
        help="Recorded release fields + derived leg and single next step "
             "(read-only; the resume decision for a scheduled relaunch)",
    )
    release_status_parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the derived position (leg, next_step, receipts) as JSON",
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
        old_rel = run_state.run_rel_for_dir_name(old_dir_name)
        if old_rel and old_rel not in rels:
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


def _release_record_for_brief(rdir: Path, state) -> dict | None:
    """Release record for the resume brief (release-flow §3), or None.

    Loaded only for release runs — every other run passes None into
    decide() and its brief stays byte-identical. An absent release.yaml is
    a fresh release run: the seed still derives a position (leg1-bump).
    Read-only and fail-soft: an unreadable record just drops out of the
    brief, like the ledger."""
    if not isinstance(state, dict) or state.get("taskdef") != "release":
        return None
    try:
        return release_run.load_release(rdir) or release_run.seed_release()
    except (run_state.RunStateError, OSError) as exc:
        print(f"⚠️  release record unreadable — brief omits it. ({exc})", file=sys.stderr)
        return None


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
        release=_release_record_for_brief(rdir, state),
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


def _work_repo_root() -> Path:
    """The work-repo checkout the claim verbs run their git plumbing in."""
    return Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))


def _cas_stage_rels(extra_rels: list[str] | None = None) -> list[str]:
    """The work-repo-relative paths the CAS verbs stage, filtered to what
    ``git add`` accepts (a stale bare-slug candidate left behind by a
    run-dir rename would otherwise make ``git add`` exit 128 and fail every
    claim/unclaim/abort attempt outright). `extra_rels` carries paths the
    resolver cannot name — above all the dir a terminal run was rolled
    aside to, which must land in the SAME commit as the fresh run that took
    its address. The specs-index file rides along for the same reason it
    does at session end: a dirty tracked file anywhere in the staged set
    would make ``pull --rebase`` refuse."""
    rels = run_state.run_rel_path_candidates()
    for extra in extra_rels or []:
        if extra not in rels:
            rels.append(extra)
    specs_rel = specs_index.specs_rel_path()
    if specs_rel and specs_rel not in rels:
        rels.append(specs_rel)
    return stageable_paths(_work_repo_root(), rels)


def _sync_remote_head() -> tuple[bool, str]:
    """Fetch and integrate the remote head (claim protocol step 1, §7).

    The claim is evaluated against the REMOTE head only — never the
    clone's possibly-stale view — so every CAS attempt starts by fetching
    and fast-forwarding/rebasing the local branch onto it. Local run-dir
    changes are snapshot-committed FIRST (commit-first ordering, same as
    commit_work_path): the session hooks routinely leave state.yaml /
    events.jsonl dirty (session-start writes owner without committing), and
    a dirty tracked tree makes ``pull --rebase`` refuse outright — exactly
    on the re-entry path the claim verbs exist for. The snapshot is an
    ordinary run-state commit, not a claim commit, so rebasing it onto the
    remote head is the normal integration path; the §7 invariant (a CLAIM
    commit is never rebased onto an unchecked head) is preserved because
    the rebase runs only while no claim commit exists locally (the
    lost-race path drops ours before looping back). The snapshot also makes
    the lost-race ``reset --hard`` genuinely safe: every pre-existing file
    — tracked or previously untracked (e.g. an uncommitted retro.md) — is
    committed BEFORE the pre-claim head is captured, so dropping the claim
    commit can never destroy anything the verb did not itself write.
    """
    repo = _work_repo_root()
    ok, detail = _commit_claim_write(
        "run-state: local snapshot before release CAS sync"
    )
    if not ok:
        return False, f"pre-sync snapshot failed: {detail}"
    rc, output = run_git_command(["fetch"], repo, check=False)
    if rc != 0:
        return False, f"git fetch failed: {output}"
    rc, output = run_git_command(["pull", "--rebase"], repo, check=False)
    if rc != 0:
        # Leave nothing half-rebased behind — a stranded rebase would block
        # every later work-repo write in this session.
        run_git_command(["rebase", "--abort"], repo, check=False)
        return False, f"git pull --rebase failed: {output}"
    return True, ""


def _git_head() -> str:
    """Local HEAD sha, or "" when it cannot be resolved."""
    rc, output = run_git_command(["rev-parse", "HEAD"], _work_repo_root(), check=False)
    return output.strip() if rc == 0 else ""


def _commit_claim_write(
    message: str, extra_rels: list[str] | None = None
) -> tuple[bool, str]:
    """Stage and commit the run dir locally — commit ONLY, never a push.

    The push half of the CAS belongs to git_ops.claim_push_once; routing
    this through commit_work_path would re-enter the rebase-retry push
    path that §7 exists to bypass.
    """
    repo = _work_repo_root()
    rels = _cas_stage_rels(extra_rels)
    if not rels:
        return True, ""
    rc, output = run_git_command(["add", "-A", "--", *rels], repo, check=False)
    if rc != 0:
        return False, f"git add failed: {output}"
    rc, output = run_git_command(
        ["status", "--porcelain", "--", *rels], repo, check=False
    )
    if not output.strip():
        # Content already committed (e.g. a same-second refresh) — the CAS
        # push still arbitrates below; nothing to commit is not a failure.
        return True, ""
    rc, output = run_git_command(["commit", "-m", message], repo, check=False)
    if rc != 0:
        return False, f"git commit failed: {output}"
    return True, ""


def _drop_claim_commit(pre_head: str) -> None:
    """Roll the local claim commit back after a failed CAS push (§7).

    The invariant: a claim commit is NEVER rebased onto a remote head that
    has not been re-checked for a foreign claim — so the loop discards the
    commit and rebuilds the claim from the re-fetched head each attempt.
    ``reset --hard`` is safe exactly here: _sync_remote_head snapshot-
    committed every pre-existing change (tracked AND untracked) before
    ``pre_head`` was captured, so the dropped commit carries only what this
    verb itself just wrote.
    """
    if pre_head:
        run_git_command(["reset", "--hard", pre_head], _work_repo_root(), check=False)


def _cas_commit_and_push(
    message: str, extra_rels: list[str] | None = None
) -> tuple[str, str]:
    """The commit+push leg of one CAS attempt, shared by claim/unclaim/
    abort (and the session-end release): commit the verb's write, issue the
    single arbitration push, and on ANY non-won outcome drop the local
    claim commit before returning.

    Dropping on CLAIM_PUSH_ERROR — not just on the lost race — matters:
    a claim commit left behind after a transport/auth failure would be
    silently rebase-pushed onto an un-re-checked head by the next ordinary
    verb's _push_with_rebase_retries, installing a dead session's claim
    over whatever landed remotely in between (the exact §7 invariant the
    CAS exists to enforce).

    Returns claim_push_once's ``(outcome, detail)``; a failed local commit
    reports as (CLAIM_PUSH_ERROR, detail) so callers fail closed on it the
    same way.
    """
    pre_head = _git_head()
    ok, detail = _commit_claim_write(message, extra_rels)
    if not ok:
        return CLAIM_PUSH_ERROR, detail
    outcome, detail = claim_push_once(_work_repo_root())
    if outcome != CLAIM_PUSH_WON:
        _drop_claim_commit(pre_head)
    return outcome, detail


def _pointer_with_url(pointer: dict) -> dict:
    """The loser's pointer plus the run dir's web URL (§7 — the kernel
    stays git-unaware, so the CLI adds the clickable form here)."""
    pointer = dict(pointer)
    run_dir_path = pointer.get("run_dir")
    pointer["web_url"] = web_url_for(run_dir_path) if run_dir_path else None
    return pointer


def _print_claim_pointer(pointer: dict) -> None:
    """Print the loser's pointer (§7) to stderr: everything needed to find
    the active release without archaeology."""
    age = pointer.get("age_minutes")
    age_text = f" ({int(age)} min ago)" if age is not None else ""
    print(f"   Run:     {pointer.get('slug')} (status: {pointer.get('status')}, "
          f"phase: {pointer.get('phase')})", file=sys.stderr)
    print(f"   Run dir: {pointer.get('run_dir')}", file=sys.stderr)
    if pointer.get("web_url"):
        print(f"   Web:     {pointer['web_url']}", file=sys.stderr)
    print(f"   Holder:  session {pointer.get('holder')}, "
          f"claimed_at {pointer.get('claimed_at')}{age_text}", file=sys.stderr)


def _claim_fail_closed(detail: str, as_json: bool = False) -> int:
    """Fail-closed refusal (§7): the claim could not be established — a
    release never proceeds unlocked. Always non-zero."""
    if as_json:
        print(json.dumps({"result": "fail-closed", "detail": detail},
                         ensure_ascii=False))
    print(f"❌ Could not establish release claim (fail closed): {detail}",
          file=sys.stderr)
    return 1


def _report_claim_lost(pointer: dict, as_json: bool, message: str) -> int:
    """Report a refused claim: the active-run pointer, exit non-zero —
    the exit code the release taskdef's refusal keys on."""
    pointer = _pointer_with_url(pointer)
    if as_json:
        print(json.dumps({"result": "lost", **pointer}, ensure_ascii=False))
    print(f"❌ {message}", file=sys.stderr)
    _print_claim_pointer(pointer)
    return 1


def _roll_over_terminal_release(
    rdir: Path, state: dict
) -> tuple[Path, dict, tuple[str, ...]]:
    """Move a FINISHED release run off the bare address and seed the next
    release run there — the claim-side half of version-in-slug.

    `work release record` normally gives a release run its version-bearing
    address while leg 1 is still running, but some runs never get there: a
    release aborted before the version was recorded, a session that died
    after completing but before its re-slug pushed, a run closed out by
    hand with `work state set --status=complete`. Each leaves a terminal run
    sitting at the address the NEXT release derives — and a terminal run is
    refused forever (correctly: it holds no live lock). This is the step
    that makes "the next release is a NEW run" true by construction rather
    than by prose (RUN-STATE.md §7, release-resume).

    The aside slug names the version when one was recorded, else takes the
    compact-UTC stamp (release_run.release_slug). Both writes stay inside
    the caller's CAS attempt so the roll-over and the fresh claim land in
    ONE commit: two racing sessions cannot both win, and the loser re-syncs
    onto the winner's fresh run and refuses against its live claim exactly
    as before.

    Returns the run to claim plus BOTH ends of the move as dir names for the
    caller to stage — the address vacated and the address moved TO. The
    destination is the load-bearing half: once the fresh run owns the
    address, `run_rel_path_candidates()` resolves the SUCCESSOR, so no
    resolver can name the aside dir and staging only the vacated path would
    make the commit a pure deletion of the previous release run's record.
    A roll-over that cannot free the address returns the original run
    unchanged and nothing to stage, and the caller refuses as it always did.
    """
    base = state.get("slug") or run_state.derive_slug()
    try:
        release = release_run.load_release(rdir)
    except (run_state.RunStateError, OSError):
        release = None  # unreadable record: the stamped form still frees it
    version = release.get("version") if isinstance(release, dict) else None
    # Uniquified, not merely version-bearing: a declined release parks a
    # terminal run on `<base>-v<X.Y.Z>` and its successor re-uses that same
    # version (RELEASE-FLOW.md §6), so the obvious aside is exactly the one
    # already taken. Computing an address that is free by construction is
    # what keeps this roll-over from dead-ending in the refusal below.
    try:
        aside = release_run.unique_release_slug(base, version, rdir.parent, rdir)
    except release_run.ReleaseRunError as exc:
        # No free address exists (pathological runs/ tree). Fail closed: the
        # caller refuses rather than claiming a terminal run.
        print(f"⚠️  roll-over skipped: {exc}", file=sys.stderr)
        return rdir, state, ()
    moved, moved_state, old_dir_name = run_state.reslug_run(rdir, state, aside)
    if moved_state.get("slug") != aside:
        return rdir, state, ()  # address not freed — caller refuses
    fresh_rdir, fresh_state = run_state.seed_run_dir(
        base,
        os.environ.get("LMER_TASK", "default"),
        os.environ.get("LMER_TASK_TARGET", ""),
        note=f"successor of {aside} ({state.get('stop_reason') or 'finished'})",
    )
    # stderr: --json callers parse stdout, and this is a warning either way.
    print(f"⚠️  previous release run '{base}' is "
          f"{state.get('status')} — rolled aside to '{aside}'; "
          f"claiming a fresh run at '{base}'", file=sys.stderr)
    # The destination goes back unconditionally, the vacated path only when a
    # rename actually happened: a re-slug interrupted between its rename and
    # its slug write is completed here from the dir it already sits in, which
    # vacates nothing but still writes state the resolver cannot name.
    moved_names = tuple(n for n in (old_dir_name, moved.name) if n)
    return fresh_rdir, fresh_state, moved_names


def cmd_release_claim(as_json: bool = False) -> int:
    """Execute `work release claim` (RUN-STATE.md §7 — frozen verb table).

    The single-flight release claim, made atomic by the claim-by-push CAS:
    fetch and evaluate the REMOTE head, write the claim through the single
    writer (run_state.claim_run), commit, then ONE plain push
    (git_ops.claim_push_once — never the rebase-retry path). A
    non-fast-forward rejection re-fetches and re-evaluates, bounded to
    RELEASE_CLAIM_ATTEMPTS.

    A resolved run that is already FINISHED is not refused: it is the
    previous release, parked on the address this one derives, so it is
    rolled aside to its version-bearing slug and a fresh run is seeded and
    claimed in the same CAS commit (_roll_over_terminal_release). That is
    what makes a second release of a repository possible at all.

    Exit 0 = claim held: fresh win, holder refresh, loud stale-claim
    takeover, or a claim on the successor of a rolled-over run. Exit
    non-zero = a live foreign claim holds the run (the loser's pointer
    prints — slug, run dir, web URL, holder session, claimed_at/age), a
    finished run whose address could not be freed (nothing claimed, nothing
    written), or the claim could not be established (remote unreachable /
    attempts exhausted): fail closed, never proceed unlocked.
    """
    if run_state.run_dir() is None:
        print("❌ No run context: LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1
    session = run_state.current_session_id()
    last_detail = "no push attempted"
    for _attempt in range(RELEASE_CLAIM_ATTEMPTS):
        ok, detail = _sync_remote_head()
        if not ok:
            return _claim_fail_closed(detail, as_json)
        # The remote head was just integrated — this load IS the remote
        # view (auto-seeds a fresh release run, mutating-verb style).
        rdir, state = _require_run()
        if rdir is None:
            return 1
        rolled_aside: str | None = None
        extra_rels: list[str] | None = None
        if state.get("status") != "in-progress":
            # A terminal run at this address is not this release — it is the
            # PREVIOUS one, still parked on an address run identity derives
            # deterministically. Roll it aside and seed the successor here,
            # inside this CAS attempt, so the next release is a new run
            # instead of a permanent refusal (version-in-slug).
            finished_slug = state.get("slug")
            rdir, state, moved_names = _roll_over_terminal_release(rdir, state)
            if state.get("status") == "in-progress":
                rolled_aside = finished_slug
                # BOTH ends of the move ride in this commit: the vacated path
                # so its deletion is recorded, and the dir the run moved TO —
                # which the resolver cannot name now that the successor holds
                # the address, so nothing else would ever add it back.
                rels = [run_state.run_rel_for_dir_name(n) for n in moved_names]
                extra_rels = [rel for rel in rels if rel] or None
        if state.get("status") != "in-progress":
            # The roll-over could not free the address (rename refused, fs
            # error). A finished run has released the lock by rule (§7 —
            # claim_status reads any claim on a non-in-progress run as
            # unclaimed), so a claim written here would be an inert block and
            # "claim taken" would misreport a live lock on a dead release.
            # Refuse WITHOUT writing, with a message the resuming session can
            # tell apart from the live-holder refusal.
            #
            # ABORTED RUNS ARE NOT EXEMPT, here or above. Re-claiming a
            # terminal run AS ITSELF would hand out a lock with NO MUTUAL
            # EXCLUSION: claim_run cannot refuse on a run whose status is not
            # in-progress (claim_status reads every claim on it as unclaimed)
            # and never restores in-progress, so two sessions would both be
            # told "claim taken" and both drive leg 1 — the CAS push does not
            # save it, since the loser re-syncs, still reads unclaimed, and
            # wins the retry. The roll-over above needs no exemption either:
            # it claims a FRESH in-progress run, so claim_run arbitrates
            # normally. An aborted run stays terminal (release_run.derive_leg
            # reports next_step None for it permanently) and its successor's
            # ctl dry-run detects the bump already on prep-release and skips
            # it (RELEASE-FLOW.md §6).
            aborted = state.get("stop_reason") == "aborted"
            detail = ("run was aborted — a later release is a NEW run"
                      if aborted else "run is complete — nothing to claim")
            if as_json:
                print(json.dumps({
                    "result": "not-live",
                    "detail": detail,
                    "slug": state.get("slug"),
                    "status": state.get("status"),
                    "stop_reason": state.get("stop_reason"),
                    "run_dir": str(rdir),
                    "web_url": web_url_for(rdir),
                }, ensure_ascii=False))
            # `detail` is a full sentence (the --json form carries it on its
            # own), so the run is named alongside it rather than in front of
            # it — "run 'X' is run is complete" was the operator-facing text
            # on the fail-closed path.
            print(f"❌ {detail} (run '{state.get('slug')}'; no live session "
                  f"holds this release; nothing was written)", file=sys.stderr)
            if aborted:
                print("   The bump commit stays on prep-release; the next "
                      "release run's dry-run detects and skips it.",
                      file=sys.stderr)
            return 1
        prior = run_state.claim_status(state, session)
        try:
            state = run_state.claim_run(rdir, state, session)
        except run_state.ClaimRefusedError as exc:
            return _report_claim_lost(exc.pointer, as_json, str(exc))
        except (run_state.RunStateError, OSError) as exc:
            return _claim_fail_closed(str(exc), as_json)
        outcome, detail = _cas_commit_and_push(
            f"run-state: {state['slug']} release claim", extra_rels
        )
        if outcome == CLAIM_PUSH_WON:
            action = {
                run_state.CLAIM_OURS: "refresh",
                run_state.CLAIM_FOREIGN_STALE: "takeover",
            }.get(prior["verdict"], "claim")
            claim = state.get("claim") or {}
            if as_json:
                payload = {
                    "result": "won",
                    "action": action,
                    "slug": state.get("slug"),
                    "session_id": claim.get("session_id"),
                    "claimed_at": claim.get("claimed_at"),
                    "run_dir": str(rdir),
                    "web_url": web_url_for(rdir),
                }
                if action == "takeover":
                    payload["displaced_session"] = prior["holder"]
                if rolled_aside:
                    payload["rolled_over"] = rolled_aside
                print(json.dumps(payload, ensure_ascii=False))
                return 0
            if action == "takeover":
                # Loud by contract (§7): the displaced session is named.
                age = prior["age_minutes"]
                age_text = (f"{int(age)} min old" if age is not None
                            else "unreadable claimed_at")
                print(f"⚠️  stale-claim takeover: displaced session "
                      f"{prior['holder']} ({age_text})")
            verb = "refreshed" if action == "refresh" else "taken"
            print(f"✅ Release claim {verb}: {state.get('slug')} "
                  f"(session {claim.get('session_id')})")
            return 0
        if outcome != CLAIM_PUSH_LOST_RACE:
            return _claim_fail_closed(f"claim push failed: {detail}", as_json)
        # Non-fast-forward rejection: the remote advanced between fetch and
        # push. The claim commit is already dropped (never rebase it onto
        # an unchecked head) — go re-evaluate the new head (§7 step 5).
        last_detail = detail
    return _claim_fail_closed(
        f"push attempts exhausted after {RELEASE_CLAIM_ATTEMPTS} "
        f"non-fast-forward rejections: {last_detail}",
        as_json,
    )


def cmd_release_claim_status(as_json: bool = False) -> int:
    """Execute `work release claim-status` (§7 frozen verb table).

    Read-only: the claim verdict on the LOCAL state — holder session,
    claimed_at, age, live/stale — or unclaimed. ALWAYS exits 0 (read-only
    convention, like `work ledger`); a broken state layer degrades to a
    warning, never a failure.
    """
    rdir = run_state.run_dir()
    if rdir is None:
        if as_json:
            print(json.dumps({"verdict": None, "detail": "no run context"}))
        else:
            print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
        return 0
    try:
        state = run_state.load_state(rdir)
    except (run_state.RunStateError, OSError) as exc:
        print(f"⚠️  {exc}", file=sys.stderr)
        return 0
    status = run_state.claim_status(state, run_state.current_session_id())
    if as_json:
        payload = dict(status)
        payload["slug"] = state.get("slug") if isinstance(state, dict) else None
        payload["run_dir"] = str(rdir)
        payload["web_url"] = web_url_for(rdir)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    if status["verdict"] == run_state.CLAIM_UNCLAIMED:
        print("Release claim: unclaimed")
        if status["holder"]:
            # A claim block on a run that is no longer in-progress reads as
            # unclaimed (§7) — surface the inert block for the runbook.
            print(f"  (inactive claim block: session {status['holder']}, "
                  f"claimed_at {status['claimed_at']} — run not in-progress)")
        return 0
    age = status["age_minutes"]
    age_text = f"{int(age)} min ago" if age is not None else "age unknown"
    print(f"Release claim: {status['verdict']} — session {status['holder']}, "
          f"claimed_at {status['claimed_at']} ({age_text})")
    if status["verdict"] == run_state.CLAIM_FOREIGN_LIVE:
        print("  Enforced single-flight: `work release claim` refuses while "
              "this claim is live.")
    elif status["verdict"] == run_state.CLAIM_FOREIGN_STALE:
        print(f"  Past {run_state.RELEASE_CLAIM_STALE_MINUTES} min — the next "
              "`work release claim` takes over loudly.")
    return 0


def cmd_release_unclaim(force: bool = False) -> int:
    """Execute `work release unclaim` (§7 frozen verb table).

    Releases our claim through the same CAS-push discipline as taking it
    (the unclaim write must land atomically on the remote too). A foreign
    claim refuses (exit 1) unless --force — the human runbook: abort path,
    stranded-claim cleanup. No claim recorded is an idempotent no-op
    success. Mutating-verb rules: no run context is an error (exit 1).
    """
    if run_state.run_dir() is None:
        print("❌ No run context: LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1
    session = run_state.current_session_id()
    last_detail = "no push attempted"
    for _attempt in range(RELEASE_CLAIM_ATTEMPTS):
        ok, detail = _sync_remote_head()
        if not ok:
            return _claim_fail_closed(detail)
        rdir = run_state.run_dir()
        try:
            state = run_state.load_state(rdir)
        except (run_state.RunStateError, OSError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        claim = state.get("claim") if isinstance(state, dict) else None
        if not isinstance(claim, dict) or not claim.get("session_id"):
            print("✅ No release claim recorded — nothing to release")
            return 0
        holder = claim.get("session_id")
        try:
            state = run_state.unclaim_run(rdir, state, session, force=force)
        except run_state.ClaimRefusedError as exc:
            return _report_claim_lost(exc.pointer, False, str(exc))
        except (run_state.RunStateError, OSError) as exc:
            return _claim_fail_closed(str(exc))
        outcome, detail = _cas_commit_and_push(
            f"run-state: {state['slug']} release unclaim"
        )
        if outcome == CLAIM_PUSH_WON:
            if holder != session:
                print(f"✅ Release claim force-released "
                      f"(was held by session {holder})")
            else:
                print(f"✅ Release claim released: {state.get('slug')}")
            return 0
        if outcome != CLAIM_PUSH_LOST_RACE:
            return _claim_fail_closed(f"unclaim push failed: {detail}")
        last_detail = detail
    return _claim_fail_closed(
        f"push attempts exhausted after {RELEASE_CLAIM_ATTEMPTS} "
        f"non-fast-forward rejections: {last_detail}"
    )


def cmd_release_abort(reason: str | None = None, force: bool = False) -> int:
    """Execute `work release abort [--reason] [--force]` (release-flow spec §7: the
    abandoned release — bump merged, human declines the release MR).

    Composes the two terminal writes plus one CAS push:

    - release_run.record_abort marks release.yaml terminal FIRST (every
      recorded field survives — the bump-MR merge SHA is what lets the
      next run's ctl dry-run skip the already-done bump), then
    - run_state.abort_run flips status/stop_reason/claim in ONE atomic
      state write — the write that releases the single-flight lock (§7:
      a claim is valid only while the run is in-progress).

    That order is the crash-safe one: dying in between leaves an
    in-progress run whose record already says aborted, and the re-run
    converges (record_abort no-ops, abort_run completes) — never a
    lock-free run whose record still asks for a next leg. The pair lands
    on the remote through the same CAS-push discipline as claim/unclaim
    (the lock transition must land atomically there too). Already aborted
    is an idempotent no-op success (nothing written or pushed); a run
    that finished any other way refuses (exit 1) BEFORE either write —
    aborting it would falsify its recorded outcome.

    A LIVE FOREIGN claim refuses (exit 1) unless `--force`, and the check
    runs BEFORE record_abort so a refused abort never marks the release
    record terminal. Without it, a session correctly refused at
    `work release claim` could take the decline path and mark another
    session's in-flight release terminal — freeing its lock remotely. A
    stale foreign claim still clears without `--force` (that is the
    takeover case). The bump commit stays on `prep-release`; aborting
    never reverts anything.
    """
    if run_state.run_dir() is None:
        print("❌ No run context: LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1
    last_detail = "no push attempted"
    for _attempt in range(RELEASE_CLAIM_ATTEMPTS):
        ok, detail = _sync_remote_head()
        if not ok:
            return _claim_fail_closed(detail)
        rdir = run_state.run_dir()
        try:
            state = run_state.load_state(rdir)
        except (run_state.RunStateError, OSError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        if state is None:
            print("❌ No release run recorded — nothing to abort", file=sys.stderr)
            return 1
        if state.get("status") != "in-progress":
            # The abort_run guard, checked here BEFORE record_abort so a
            # refused abort never marks the release record terminal.
            if state.get("stop_reason") == "aborted":
                print(f"✅ Release run already aborted: {state.get('slug')} "
                      f"— nothing to do")
                return 0
            print(f"❌ run '{state.get('slug')}' is already "
                  f"{state.get('status')} (stop_reason: "
                  f"{state.get('stop_reason')}) — nothing to abort",
                  file=sys.stderr)
            return 1
        claim = state.get("claim")
        holder = claim.get("session_id") if isinstance(claim, dict) else None
        session = run_state.current_session_id()
        # Evaluated BEFORE record_abort: a refused abort must leave the
        # release record untouched.
        if run_state.is_claimed_by_other(state, session) and not force:
            status = run_state.claim_status(state, session)
            return _report_claim_lost(
                run_state.claim_pointer(rdir, state, session),
                False,
                f"run '{state.get('slug')}' is claimed by session "
                f"{status['holder']} ({int(status['age_minutes'])} min ago) — "
                f"refusing to abort a release another session is driving "
                f"(pass --force if that session is known dead)",
            )
        try:
            release_run.record_abort(rdir, reason=reason)
            state = run_state.abort_run(
                rdir, state, reason=reason, session=session, force=force)
        except (run_state.RunStateError, run_state.ClaimRefusedError, OSError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        outcome, detail = _cas_commit_and_push(
            f"run-state: {state['slug']} release abort"
        )
        if outcome == CLAIM_PUSH_WON:
            cleared = f" (claim cleared: session {holder})" if holder else ""
            print(f"✅ Release run aborted: {state.get('slug')}{cleared}")
            # Precise about WHERE the next release goes: this run keeps its
            # address until the next `work release claim` rolls it aside —
            # the abort is a terminal write and nothing else.
            print("   Bump commit stays on prep-release; the next "
                  "`work release claim` rolls this run aside and starts the "
                  "next release run.")
            return 0
        if outcome != CLAIM_PUSH_LOST_RACE:
            return _claim_fail_closed(f"abort push failed: {detail}")
        last_detail = detail
    return _claim_fail_closed(
        f"push attempts exhausted after {RELEASE_CLAIM_ATTEMPTS} "
        f"non-fast-forward rejections: {last_detail}"
    )


def _ensure_release_slug(
    rdir: Path, state: dict, release: dict | None
) -> tuple[Path, dict, str | None]:
    """Move a release run to its version-bearing address once the version is
    recorded (spec: version-in-slug; run_state.reslug_run).

    Run identity is deterministic per `(taskdef, target)`, so a release run
    that kept the derived slug forever meant the SECOND release of a
    repository resolved to the first one's finished run and was refused
    permanently. Leg 1 records the version before the bump branch leaves the
    machine, so that is the earliest point the run can be given an address of
    its own — and the bare address is free from then on.

    Called after EVERY release-record verb, not just `record version`: the
    re-slug can fail (target taken, fs error) or be interrupted between its
    rename and its slug write, and retrying at the next leg-1 step heals it
    instead of carrying a stale address to the end of the release. Already
    at the target slug is a no-op. The base is derived from the run's OWN
    recorded taskdef/target rather than the ambient env, so the address a
    run moves to never depends on who invoked the verb.
    """
    version = release.get("version") if isinstance(release, dict) else None
    if not version:
        return rdir, state, None
    base = run_state.derive_slug(state.get("taskdef"), state.get("target"))
    # "Already moved?" is asked of the VERSION, not of one exact string: the
    # address may be a uniquified variant (a re-used version — §6's declined
    # release), and comparing against the canonical form would make this run
    # look un-moved and re-slug it to a fresh address at every record verb.
    if release_run.names_version(state.get("slug"), base, version):
        return rdir, state, None
    try:
        target = release_run.unique_release_slug(base, version, rdir.parent, rdir)
    except release_run.ReleaseRunError as exc:
        # No address is free (pathological runs/ tree). Same fallback a failed
        # rename takes: the record itself already landed, so warn and leave the
        # run where it is — the next record verb retries the move.
        print(f"⚠️  re-slug skipped: {exc}", file=sys.stderr)
        return rdir, state, None
    return run_state.reslug_run(rdir, state, target)


def cmd_release_record(args) -> int:
    """Execute `work release record <field>` (§7 frozen verb table —
    release-flow spec §3).

    Every mutation goes through the release.yaml single writer (the
    release_run recorders): identity fields (version, merge SHAs, tag) are
    write-once — re-recording the identical value is an idempotent no-op so
    re-entered legs converge, while a value that contradicts the record is
    refused with the kernel's hard-stop message (exit 1; recorded release
    identity never silently moves). Receipts may be re-recorded (a
    re-dispatched Actions run replaces the URL). Every actual write appends
    a `release` audit event in the kernel, and the run dir is pushed after
    the verb (durability, non-fatal) — the record is exactly what a
    relaunched or scheduled session resumes from. Mutating-verb rules:
    no run context is an error (exit 1); the run auto-seeds.
    """
    field = args.record_field
    if field is None:
        print("❌ release record requires a field: version | bump-sha | "
              "merge-sha | tag | receipt", file=sys.stderr)
        return 1
    rdir, state = _require_run()
    if rdir is None:
        return 1
    try:
        if field == "version":
            release = release_run.record_version(rdir, args.value)
            noun = f"version {release['version']}"
        elif field == "bump-sha":
            release = release_run.record_bump_merge(rdir, args.value)
            noun = f"bump-sha {release['bump_mr_merge_sha'][:12]}"
        elif field == "merge-sha":
            release = release_run.record_release_merge(
                rdir, args.value, args.observed_version
            )
            noun = f"merge-sha {release['release_mr_merge_sha'][:12]}"
        elif field == "tag":
            release = release_run.record_tag(rdir, args.value, args.sha)
            noun = f"tag {release['tag']['name']}"
        else:  # receipt
            release = release_run.record_receipt(
                rdir, args.value, url=args.url, note=args.note
            )
            noun = f"receipt {args.value}"
        derived = release_run.derive_leg(release)
    except (run_state.RunStateError, OSError) as exc:
        # ReleaseRunError included — the kernel's hard-stop message prints
        # verbatim (version mismatch at the merge SHA, tag drift, a
        # contradicted write-once field) and nothing was overwritten.
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    # The run takes its version-bearing address the moment the version is
    # recorded — this is what makes the NEXT release a new run rather than a
    # permanent refusal on this one.
    rdir, state, old_dir_name = _ensure_release_slug(rdir, state, release)
    print(f"✅ Release record: {noun}")
    print(f"   Position: {derived['leg']} — next: {derived['next_step']}")
    if old_dir_name:
        print(f"   Run dir:  {rdir} (run is now '{state['slug']}'; cite the "
              f"new path — the old one is free for the next release)")
    # Durability: the record is the crash-recovery contract (spec §3 —
    # resume is the contract) — push now, not at session end. An idempotent
    # no-op stages nothing and commit_work_path skips the empty commit. The
    # pre-rename path rides along so a re-slug's deletion lands too.
    _push_run_dir(state, f"release record {noun}", old_dir_name,
                  saved="release record")
    return 0


def cmd_release_status(as_json: bool = False) -> int:
    """Execute `work release status` (§7 frozen verb table).

    Read-only: the recorded fields plus the derived leg and the SINGLE next
    step — the decision a relaunched or scheduled session keys on (spec §3:
    exit immediately when there is nothing to advance). No run context and
    no release recorded yet are both normal (exit 0); the JSON form still
    derives a position from an empty record (leg1-bump) so the caller
    always gets a next_step. An unreadable record or an internally
    inconsistent one (hand-edited tag vs merge SHA) exits 1 with the
    kernel's hard-stop message — never converge over it.
    """
    rdir = run_state.run_dir()
    if rdir is None:
        if as_json:
            print(json.dumps({"leg": None, "next_step": None,
                              "detail": "no run context"}))
        else:
            print("No run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
        return 0
    try:
        release = release_run.load_release(rdir)
        if as_json:
            payload = release_run.derive_leg(release)
            payload["recorded"] = release is not None
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(release_run.format_release_status(release))
        return 0
    except (run_state.RunStateError, OSError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


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


def _apply_pushed_answer(rdir: Path, state: dict) -> tuple[dict, dict | None]:
    """Apply a pushed LMER_ANSWER to the run's recorded open question (issue #98); returns (state, answered)."""
    # Resume-on-answer: a pushed answer (LMER_ANSWER, set by
    # `lmer --answer "<text>"` on the host) to the run's recorded open
    # question is applied BEFORE deciding, so the brief leads with the
    # answered pair instead of the stale question block. Fail-soft: a
    # problem applying it degrades to the plain brief, never a failure.
    # Consume-once: _answer_marker_path guards against the container-lived
    # env var replaying into a later question (review on !126).
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
    return state, answered


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

        state, answered = _apply_pushed_answer(rdir, state)

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
            release=_release_record_for_brief(rdir, state),
        )

        run_state.append_event(rdir, "session_start")
        if run_state.is_claimed_by_other(state, run_state.current_session_id()):
            # A LIVE foreign release claim (RUN-STATE.md §7) is enforced,
            # not advisory: a session starting on a claimed release run
            # must not silently steal the lock by writing itself in as
            # owner. The brief below already carries the enforced-claim
            # warning; `work release claim` is the only arbitration path.
            holder = (state.get("claim") or {}).get("session_id")
            print(f"⚠️  release claim held by session {holder} — owner not "
                  f"taken (enforced single-flight; `work release claim` "
                  f"arbitrates)")
        else:
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


def _release_claim_at_session_end(rdir) -> None:
    """Release a release claim THIS session holds, at clean session end (§7).

    Without this, a cleanly-ended holder strands a live-looking claim that
    blocks legitimate relaunches for up to RELEASE_CLAIM_STALE_MINUTES —
    the stale threshold exists for CRASHED sessions, not clean exits. Runs
    the same CAS discipline as `work release unclaim` (sync the remote
    head, re-check the claim is still ours on THAT head — a takeover may
    have displaced us — write, one arbitration push), but strictly
    best-effort and quiet: session-end is a hook (always exits 0), so any
    failure degrades to a warning and the stale threshold remains the
    backstop.
    """
    session = run_state.current_session_id()
    for _attempt in range(RELEASE_CLAIM_ATTEMPTS):
        ok, detail = _sync_remote_head()
        if not ok:
            print(f"⚠️  session-end claim release skipped: {detail}")
            return
        try:
            state = run_state.load_state(rdir)
        except (run_state.RunStateError, OSError):
            return
        if run_state.claim_status(state, session)["verdict"] != run_state.CLAIM_OURS:
            return  # nothing of ours to release on the remote head
        try:
            state = run_state.unclaim_run(rdir, state, session)
        except (run_state.ClaimRefusedError, run_state.RunStateError, OSError) as exc:
            print(f"⚠️  session-end claim release failed: {exc}")
            return
        outcome, detail = _cas_commit_and_push(
            f"run-state: {state['slug']} release unclaim (session end)"
        )
        if outcome == CLAIM_PUSH_WON:
            print(f"✅ Release claim released at session end: {state.get('slug')}")
            return
        if outcome != CLAIM_PUSH_LOST_RACE:
            print(f"⚠️  session-end claim release push failed: {detail}")
            return
    print("⚠️  session-end claim release lost the CAS race repeatedly; "
          "leaving the claim to the stale threshold")


def cmd_session_end() -> int:
    """Record session end, release a claim we hold, and push the run dir.
    Hook-facing: ALWAYS exits 0."""
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
        # The single-flight release claim (§7) is a separate lock from
        # `owner` and must be released through the CAS path, not a plain
        # state write — check on the LOCAL view first so non-release
        # sessions never pay the git round-trips.
        if (run_state.claim_status(state, run_state.current_session_id())
                ["verdict"] == run_state.CLAIM_OURS):
            _release_claim_at_session_end(rdir)
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
    elif args.command == "release":
        if args.release_action == "claim":
            return cmd_release_claim(args.as_json)
        elif args.release_action == "claim-status":
            return cmd_release_claim_status(args.as_json)
        elif args.release_action == "unclaim":
            return cmd_release_unclaim(args.force)
        elif args.release_action == "abort":
            return cmd_release_abort(args.reason, force=args.force)
        elif args.release_action == "record":
            return cmd_release_record(args)
        elif args.release_action == "status":
            return cmd_release_status(args.as_json)
        parser.print_help()
        return 1
    elif args.command == "session-start":
        return cmd_session_start()
    elif args.command == "session-end":
        return cmd_session_end()
    else:
        print(f"❌ Unknown command: {args.command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
