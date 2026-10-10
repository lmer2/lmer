"""`work knowledge index` — generate `_knowledge/<org>/INDEX.md` (#372).

An indexer, not a content validator (spec §13.C): it emits one line per page
under an org, grouped by topic, and never adds a validator code.

Contract:

- **One line per page:** the first `# ` heading (falling back to the filename
  stem), a relative link, and the page's last git-touch date. A page with no
  history shows no date.
- **Sections:** a topic subdirectory becomes a `##` section; pages at the org
  root come first.
- **Frontmatter `description:`** pins the line text; nothing else in
  frontmatter is required or interpreted.
- **INDEX.md is never a page.** `_archive/_knowledge/` is never walked, so a
  page housekeeping moved drops out on the next regeneration.
- **A generated file carries the marker comment.** An existing INDEX.md
  without it is hand-written: reported and skipped, never overwritten.
- A plain run rewrites stale generated indexes in place and prints what
  changed. `--check` writes nothing and exits 1 when any generated index is
  out of date (a missing index in an org that has pages counts as out of
  date), 0 otherwise. `--check` compares everything EXCEPT the per-line git
  commit date — pages, titles, order, structure — so committing the
  regenerated index does not make it stale at once (an uncommitted page has
  no git date until the commit that adds it). An unknown `--org` exits 2.
  Neither mode commits; the caller commits.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .utils import work_repo_root

KNOWLEDGE_DIR = "_knowledge"
ARCHIVE_DIR = "_archive"
INDEX_NAME = "INDEX.md"
MARKER = "<!-- lmer:knowledge-index:generated -->"


class KnowledgeError(Exception):
    """A refused or failed knowledge operation."""


@dataclass(frozen=True)
class Page:
    path: Path          # absolute
    rel: str            # org-relative POSIX
    title: str
    description: Optional[str]
    touched: Optional[str]  # ISO date, or None with no history


def _git_last_touch(root: Path, rel: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", rel],
            cwd=str(root), capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    stamp = proc.stdout.strip()
    return stamp or None


def _frontmatter(text: str) -> dict:
    """The YAML frontmatter mapping, or {} when there is none/unparseable."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    import yaml

    try:
        data = yaml.safe_load(text[3:end])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _title(text: str, stem: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or stem
    return stem


def _read_page(org_dir: Path, page: Path, root: Path) -> Optional[Page]:
    try:
        text = page.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    rel = page.relative_to(org_dir).as_posix()
    front = _frontmatter(text)
    description = front.get("description")
    description = description if isinstance(description, str) and description.strip() else None
    return Page(
        path=page, rel=rel, title=_title(text, page.stem),
        description=description,
        touched=_git_last_touch(root, page.relative_to(root).as_posix()),
    )


def collect_pages(org_dir: Path, root: Path) -> list[Page]:
    """Every page under *org_dir*, root pages first then topic sections.

    Topics may nest: a page at ``notes/deep/x.md`` is grouped under the
    section ``notes/deep``.
    """
    pages: list[Page] = []
    for page in sorted(org_dir.rglob("*.md")):
        if page.name == INDEX_NAME or not page.is_file():
            continue
        read = _read_page(org_dir, page, root)
        if read is not None:
            pages.append(read)
    return pages


def _line(page: Page) -> str:
    text = page.description or page.title
    suffix = f" — {page.touched}" if page.touched else ""
    return f"- [{text}]({page.rel}){suffix}"


def render_index(org: str, pages: Sequence[Page]) -> str:
    """The full INDEX.md text for one org."""
    lines = [MARKER, f"# {org} knowledge", ""]
    root_pages = [p for p in pages if "/" not in p.rel]
    for page in root_pages:
        lines.append(_line(page))
    if root_pages:
        lines.append("")
    topics = {}
    for page in pages:
        if "/" in page.rel:
            topics.setdefault(page.rel.rsplit("/", 1)[0], []).append(page)
    for topic in sorted(topics):
        lines.append(f"## {topic}")
        lines.append("")
        for page in topics[topic]:
            lines.append(_line(page))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _orgs(root: Path, org: Optional[str]) -> list[Path]:
    base = root / KNOWLEDGE_DIR
    if not base.is_dir():
        if org:
            raise KnowledgeError(org)
        return []
    if org:
        target = base / org
        if not target.is_dir():
            raise KnowledgeError(org)
        return [target]
    return sorted(p for p in base.iterdir() if p.is_dir())


_DATE_SUFFIX_RE = re.compile(r" — \d{4}-\d{2}-\d{2}$", re.M)


def _strip_dates(text: str) -> str:
    """The index without its per-line git-commit dates.

    `--check` compares this so committing the regenerated index does not make
    it stale at once: an uncommitted page has no git date, and the date the
    commit gives it would otherwise change the render.
    """
    return _DATE_SUFFIX_RE.sub("", text)


def run_index(*, check: bool = False, org: Optional[str] = None,
              out=None) -> int:
    """Generate (or check) the indexes; ``0`` ok, ``1`` out of date, ``2``
    an unknown ``--org``."""
    out = out or print
    root = work_repo_root()
    try:
        org_dirs = _orgs(root, org)
    except KnowledgeError:
        out(f"❌ no knowledge org named {org!r} in {root / KNOWLEDGE_DIR}",
            file=sys.stderr)
        return 2
    changed = False
    for org_dir in org_dirs:
        name = org_dir.name
        pages = collect_pages(org_dir, root)
        index = org_dir / INDEX_NAME
        if not pages and not index.is_file():
            # Nothing to index and no generated index to fix. An EXISTING
            # marked index still runs: its last page may have been removed or
            # archived, and it must be regenerated (empty) and reported stale.
            continue
        rendered = render_index(name, pages)
        if index.is_file():
            existing = index.read_text(encoding="utf-8")
            if not existing.startswith(MARKER):
                out(f"⚠️  {index.relative_to(root)} is hand-written; skipped")
                continue
            if existing == rendered:
                continue
            if check and _strip_dates(existing) == _strip_dates(rendered):
                continue  # only git-commit dates differ: a stable round-trip
        changed = True
        if check:
            out(f"{index.relative_to(root)} is out of date")
            continue
        index.write_text(rendered, encoding="utf-8")
        out(f"✅ wrote {index.relative_to(root)}")
    if check:
        return 1 if changed else 0
    return 0


def cmd_knowledge(args) -> int:
    """The ``work knowledge`` entry point."""
    if getattr(args, "knowledge_action", None) != "index":
        print("knowledge: expected `index`", file=sys.stderr)
        return 1
    return run_index(check=getattr(args, "check", False),
                     org=getattr(args, "org", None))
