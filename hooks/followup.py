#!/Agents/global/.venv/bin/python3
"""
Hook for the /followup command.

Loads and prompts the task's follow-up instructions to Claude. The follow-up
text lives in a `followup.txt` file next to `instructions.txt` inside the
active task definition directory, and is populated per task type (e.g. the
develop task might instruct Claude to read a posted MR review and address it).

Usage:
  followup         - Read followup.txt from the current task context (LMER_TASK)
"""
import os
import sys
from pathlib import Path

_HOOKS_PARENT = str(Path(__file__).resolve().parent.parent)
if _HOOKS_PARENT not in sys.path:
    sys.path.insert(0, _HOOKS_PARENT)

from hooks.start import (  # noqa: E402
    check_task_context,
    find_taskdef_file,
    render_taskdef_template,
    taskdef_search_dirs,
)


FOLLOWUP_FILENAME = "followup.txt"


def find_followup_file(taskdef_name=None):
    """Locate the followup.txt for the active task definition."""
    resolved_name = (
        taskdef_name
        or os.environ.get("LMER_TASK")
        or os.environ.get("LMER_TASKDEF")
    )
    if not resolved_name and not (
        os.environ.get("LMER_TASKDEF_DIR") or os.environ.get("LMER_TASK_INSTRUCTIONS")
    ):
        print("❌ ERROR: No task definition specified and LMER_TASK not set")
        print("💡 Usage: followup")
        return None

    followup_file = find_taskdef_file(FOLLOWUP_FILENAME, resolved_name)
    if followup_file:
        return followup_file

    display_name = resolved_name or "<unknown>"
    search_dirs = taskdef_search_dirs()
    print(f"❌ ERROR: {FOLLOWUP_FILENAME} not found for task '{display_name}'")
    print(f"💡 Searched in: {', '.join(str(d) for d in search_dirs)}")
    print(
        "📝 Add a followup.txt file next to instructions.txt in the task "
        "definition directory to define follow-up behavior for this task type."
    )
    return None


def read_and_display_followup(followup_file):
    """Render and display the task's follow-up instructions."""
    print(f"📋 Follow-up Instructions: {followup_file.parent.name}")
    print(f"📍 Location: {followup_file}")
    print("\n" + "=" * 60)

    rendered = render_taskdef_template(
        followup_file,
        extra_context={"followup_file": str(followup_file)},
    )
    print(rendered)
    print("=" * 60 + "\n")
    return True


def main():
    """Main hook execution."""
    print("🔁 Loading follow-up instructions...\n")

    followup_file = find_followup_file()
    if not followup_file:
        sys.exit(1)

    check_task_context()

    if not read_and_display_followup(followup_file):
        sys.exit(1)

    print("✅ Follow-up instructions loaded")
    print("📋 Next: Address the follow-up items described above\n")


if __name__ == "__main__":
    main()
