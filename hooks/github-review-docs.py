#!/Agents/global/.venv/bin/python3
"""
Hook for github-review-docs command.
Reads the GitHub review tool documentation.
"""
import sys
from pathlib import Path


def find_docs_file():
    """Find the github-review docs file."""
    # Try mounted lmer-docs directory first
    lmer_docs = Path("/Agents/global/lmer-docs/GITHUB-REVIEW.md")
    if lmer_docs.exists():
        return lmer_docs

    # Try to find docs in order of preference
    lmer_global = Path("/home/developer/.lmer")
    agents_global = Path("/Agents/global")

    if lmer_global.exists():
        base_path = lmer_global
    elif agents_global.exists():
        base_path = agents_global
    else:
        # Fallback to current directory
        base_path = Path.cwd()

    docs_file = base_path / "lmer-docs" / "GITHUB-REVIEW.md"

    if docs_file.exists():
        return docs_file

    # Try alternative locations
    alt_paths = [
        base_path / "docs" / "GITHUB-REVIEW.md",
        base_path / "GITHUB-REVIEW.md",
        Path("/Agents/global/docs/GITHUB-REVIEW.md"),
        Path("/home/developer/.lmer/docs/GITHUB-REVIEW.md"),
    ]

    for alt_path in alt_paths:
        if alt_path.exists():
            return alt_path

    return None


def main():
    """Read and display github-review documentation."""
    docs_file = find_docs_file()

    if not docs_file:
        print("❌ GitHub review documentation not found", file=sys.stderr)
        print("   Expected location: lmer-docs/GITHUB-REVIEW.md", file=sys.stderr)
        return 1

    try:
        content = docs_file.read_text(encoding='utf-8')
        print(content)
        return 0
    except Exception as e:
        print(f"❌ Error reading documentation: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
