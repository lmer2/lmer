"""Read and concatenate project info files."""

from pathlib import Path
from typing import List, Optional

from .run_state import run_dir
from .utils import project_info_dir, task_info_dir, task_target_dir


def read_project_info() -> str:
    """
    Read and concatenate all .md files from project info directories.

    Reads from:
    - {host}/{project}/info/ (global project info)
    - {host}/{project}/{task_type}/info/ (project+task specific info)

    Also reports:
    - If log.yaml exists in the run dir (falling back to the legacy task
      target directory for pre-unification runs), with absolute path
    - List of report files: .md files under the run dir's reports/ plus the
      legacy task target directory, sorted by modification time (most
      recent first), with absolute paths

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

    # Log and report files live in the run dir (issue #87 D4); the legacy
    # task target directory is read-side fallback for pre-unification runs.
    tgt_dir = task_target_dir()
    rdir: Optional[Path] = run_dir()

    info_sections: List[str] = []

    # Add project info content
    if content_parts:
        info_sections.append("\n---\n\n".join(content_parts))
    else:
        info_sections.append("No info files found in project or task-specific info directories.")

    # Check for log.yaml: run dir first, legacy task target dir as fallback
    log_candidates = []
    if rdir is not None:
        log_candidates.append(rdir / "log.yaml")
    log_candidates.append(tgt_dir / "log.yaml")
    for log_file in log_candidates:
        if log_file.exists():
            info_sections.append(f"\n---\n\n## Task Related Work Log File\n\nLog file exists: {log_file.resolve()}")
            break

    # Check for report files: run dir reports/ plus the legacy location
    report_dirs = []
    if rdir is not None:
        report_dirs.append(rdir / "reports")
    report_dirs.append(tgt_dir)
    report_files = [
        f
        for report_dir in report_dirs
        if report_dir.exists() and report_dir.is_dir()
        for f in report_dir.glob("*.md")
        if f.is_file() and not f.name.startswith(".")
    ]

    if report_files:
        # Sort by modification time, most recent first
        report_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        info_sections.append("\n---\n\n## Task Related Report Files\n")
        for report_file in report_files:
            info_sections.append(f"- {report_file.resolve()}")

    return "\n".join(info_sections)
