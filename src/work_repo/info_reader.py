"""Read and concatenate project info files."""

from typing import List

from .utils import project_info_dir, task_info_dir, task_target_dir


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
    proj_info_dir = project_info_dir()
    tsk_info_dir = task_info_dir()

    if proj_info_dir is None or tsk_info_dir is None:
        return "Error: LMER_REPO_HOST and LMER_REPO_PROJECT must be set"

    content_parts: List[str] = []

    # Read project info files
    if proj_info_dir.exists() and proj_info_dir.is_dir():
        md_files = sorted(proj_info_dir.glob("*.md"))
        for md_file in md_files:
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    file_content = f.read()
                    if file_content.strip():
                        content_parts.append(f"# {md_file.name}\n\n{file_content}\n")
            except IOError as e:
                content_parts.append(f"Error reading {md_file}: {e}\n")

    # Read task-specific info files
    if tsk_info_dir.exists() and tsk_info_dir.is_dir():
        md_files = sorted(tsk_info_dir.glob("*.md"))
        for md_file in md_files:
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    file_content = f.read()
                    if file_content.strip():
                        content_parts.append(f"# {md_file.name}\n\n{file_content}\n")
            except IOError as e:
                content_parts.append(f"Error reading {md_file}: {e}\n")

    # Check for log.yaml and report files in task target directory
    tgt_dir = task_target_dir()

    info_sections: List[str] = []

    # Add project info content
    if content_parts:
        info_sections.append("\n---\n\n".join(content_parts))
    else:
        info_sections.append("No info files found in project or task-specific info directories.")

    # Check for log.yaml
    log_file = tgt_dir / "log.yaml"
    if log_file.exists():
        info_sections.append(f"\n---\n\n## Task Related Work Log File\n\nLog file exists: {log_file.resolve()}")

    # Check for report files (.md files in task target directory)
    if tgt_dir.exists() and tgt_dir.is_dir():
        # Get all .md files, excluding hidden files
        report_files = [
            f for f in tgt_dir.glob("*.md")
            if f.is_file() and not f.name.startswith(".")
        ]

        if report_files:
            # Sort by modification time, most recent first
            report_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

            info_sections.append("\n---\n\n## Task Related Report Files\n")
            for report_file in report_files:
                info_sections.append(f"- {report_file.resolve()}")

    return "\n".join(info_sections)
