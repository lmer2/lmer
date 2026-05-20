"""Read and concatenate project info files."""

import os
from pathlib import Path
from typing import List

from .utils import sanitize_task_target


def read_project_info() -> str:
    """
    Read and concatenate all .md files from project info directories.

    Reads from:
    - {host}/{project}/info/ (global project info)
    - {host}/{project}/{task_type}/info/ (project+task specific info)

    Also reports:
    - If log.yaml exists in the task target directory (with absolute path)
    - List of report files (.md files) in the task target directory, sorted by modification time (most recent first), with absolute paths

    Returns:
        Concatenated content of all .md files, plus log and report file information
    """
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    task_type = os.environ.get("LMER_TASK", "default")

    if not repo_host or not repo_project:
        return "Error: LMER_REPO_HOST and LMER_REPO_PROJECT must be set"

    # Build paths
    project_info_dir = work_repo_path / repo_host / repo_project / "info"
    task_info_dir = work_repo_path / repo_host / repo_project / task_type / "info"

    content_parts: List[str] = []

    # Read project info files
    if project_info_dir.exists() and project_info_dir.is_dir():
        md_files = sorted(project_info_dir.glob("*.md"))
        for md_file in md_files:
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    file_content = f.read()
                    if file_content.strip():
                        content_parts.append(f"# {md_file.name}\n\n{file_content}\n")
            except IOError as e:
                content_parts.append(f"Error reading {md_file}: {e}\n")

    # Read task-specific info files
    if task_info_dir.exists() and task_info_dir.is_dir():
        md_files = sorted(task_info_dir.glob("*.md"))
        for md_file in md_files:
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    file_content = f.read()
                    if file_content.strip():
                        content_parts.append(f"# {md_file.name}\n\n{file_content}\n")
            except IOError as e:
                content_parts.append(f"Error reading {md_file}: {e}\n")

    # Check for log.yaml and report files in task target directory
    task_target = os.environ.get("LMER_TASK_TARGET", "default")
    safe_task_target = sanitize_task_target(task_target) if task_target else "default"
    task_target_dir = work_repo_path / repo_host / repo_project / task_type / safe_task_target

    info_sections: List[str] = []

    # Add project info content
    if content_parts:
        info_sections.append("\n---\n\n".join(content_parts))
    else:
        info_sections.append("No info files found in project or task-specific info directories.")

    # Check for log.yaml
    log_file = task_target_dir / "log.yaml"
    if log_file.exists():
        info_sections.append(f"\n---\n\n## Task Related Work Log File\n\nLog file exists: {log_file.resolve()}")

    # Check for report files (.md files in task target directory)
    if task_target_dir.exists() and task_target_dir.is_dir():
        # Get all .md files, excluding hidden files
        report_files = [
            f for f in task_target_dir.glob("*.md")
            if f.is_file() and not f.name.startswith(".")
        ]

        if report_files:
            # Sort by modification time, most recent first
            report_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

            info_sections.append("\n---\n\n## Task Related Report Files\n")
            for report_file in report_files:
                info_sections.append(f"- {report_file.resolve()}")

    return "\n".join(info_sections)
