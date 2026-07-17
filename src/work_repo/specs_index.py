"""Central specs index (issue #101).

Specs land deep inside per-run dirs — `runs/<slug>/spec.md`, masterplan
bundle files at `runs/<slug>/masterplan/<mp>/spec.md` — so finding "that
spec from last week" means archaeology. The specs index is a flat
`{host}/{project}/specs/` directory (sibling of `info/` and `runs/`) of
dated RELATIVE symlinks pointing at each spec's canonical location: a
plain directory listing IS the index, so no README/index file is
maintained beside the links (v1 is symlinks-only by design).

Entry shape: `YYYY-MM-DD-<run>-<basename>` — the registration date, the
run's name (state.name, slug fallback) to disambiguate, and the spec's
own filename. Entry form: relative symlink (`../runs/<dir>/…`) to the
one canonical file — never a copy (the charter's "never write the same
file in two places" rule, #103) and, for masterplan bundle specs, never
the run-root convenience symlink (the bundle file is the single target).
One entry per (run, spec file): re-registering the same spec re-points
the same day's link, and a later-day re-registration replaces the older
dated entry rather than accumulating duplicates.

Spec-class predicate (is_spec_artifact): a Markdown file whose stem is
exactly `spec`, starts with `spec` + separator, or ends with separator +
`spec` (separators: `-`, `_`, `.`; case-insensitive). So `spec.md`,
`spec-auth.md`, `foo-spec.md`, `masterplan-spec.md` qualify; `plan.md`,
`spec.py` (not Markdown), `specifics.md`, `inspect.md` do not.

Every write path here is fail-soft by contract: the index is a
convenience view over runs/, and no index problem may ever fail the
artifact registration (or sync) that triggered it. `rebuild()` is the
backfill path for runs that predate the index.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .utils import _work_repo_base

__all__ = [
    "SPECS_DIR", "is_spec_artifact", "specs_dir", "specs_rel_path",
    "run_label", "upsert_spec_link", "list_entries", "rebuild",
    "repoint_run_dir_entries",
]

SPECS_DIR = "specs"

# Spec-class filename stems (see module docstring for the rationale).
_SPEC_STEM_RE = re.compile(r"^spec$|^spec[-_.]|[-_.]spec$", re.IGNORECASE)
# Entry names this module owns: date prefix + label + basename.
_DATE_PREFIX = "%Y-%m-%d"
# Run labels come from state.name/slug (already slug-shaped); anything
# else is squashed so an entry name is always a plain filename.
_UNSAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


def is_spec_artifact(name: str) -> bool:
    """True when `name` is a spec-class artifact filename (see module doc)."""
    p = Path(str(name))
    return p.suffix.lower() == ".md" and bool(_SPEC_STEM_RE.search(p.stem))


def specs_dir() -> Optional[Path]:
    """`{work}/{host}/{project}/specs/` — sibling of info/ and runs/."""
    base = _work_repo_base()
    if base is None:
        return None
    return base / SPECS_DIR


def specs_rel_path() -> Optional[str]:
    """The specs dir relative to the work-repo root, for commit staging."""
    host = os.environ.get("LMER_REPO_HOST")
    project = os.environ.get("LMER_REPO_PROJECT")
    if not host or not project:
        return None
    return f"{host}/{project}/{SPECS_DIR}"


def run_label(rdir: Path, state: Optional[dict] = None) -> str:
    """The run identity baked into an entry name: state.name, slug fallback,
    then the directory name (already `slug` or `slug--name` shaped) when no
    state is readable — an unreadable sibling must never break indexing."""
    if state is None:
        from . import run_state  # local: run_state imports this module
        state = run_state._read_sibling_state(rdir)
    if isinstance(state, dict):
        label = state.get("name") or state.get("slug")
        if label:
            return str(label)
    return rdir.name


def _entry_name(label: str, basename: str, when: Optional[datetime]) -> str:
    when = when or datetime.now(timezone.utc)
    safe = _UNSAFE_LABEL_RE.sub("-", label).strip("-") or "run"
    return f"{when.strftime(_DATE_PREFIX)}-{safe}-{basename}"


def upsert_spec_link(
    spec_path: Path,
    label: str,
    when: Optional[datetime] = None,
    alias: Optional[str] = None,
) -> Optional[Path]:
    """Upsert one specs-index entry for a spec-class file. Fail-soft.

    `spec_path` must be the spec's CANONICAL location inside the work-repo
    checkout (a run-dir file or a masterplan bundle file — callers resolve
    convenience symlinks first). `alias` overrides the basename baked into
    the entry name (still the spec-class gate): the masterplan sync passes
    its run-root link name (`mp-a-spec.md` in multi-bundle mode) so two
    bundles' `spec.md` files get distinct entries. Non-spec names, files
    outside the work repo, and any fs error are skipped (warned, never
    raised): index maintenance must never fail the registration that
    triggered it.

    Idempotent per (label, basename): the same day's entry is re-pointed
    in place, and stale entries for the same pair under other dates are
    removed, so re-registration never accumulates duplicates.

    Returns the entry path, or None when nothing was indexed.
    """
    try:
        base = _work_repo_base()
        basename = alias or spec_path.name
        if base is None or not is_spec_artifact(basename):
            return None
        spec_path = spec_path if spec_path.is_absolute() else spec_path.absolute()
        if not spec_path.is_file() or not spec_path.is_relative_to(base):
            return None
        sdir = base / SPECS_DIR
        sdir.mkdir(parents=True, exist_ok=True)
        entry = _entry_name(label, basename, when)
        # One entry per (run, spec file): drop same-pair entries under
        # other dates before (re-)pointing today's.
        safe = entry[len("YYYY-MM-DD-"):]
        stale_re = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(safe)}$")
        for sibling in sdir.iterdir():
            if sibling.name != entry and stale_re.match(sibling.name):
                if sibling.is_symlink():
                    sibling.unlink()
        target = os.path.relpath(spec_path, sdir)
        link = sdir / entry
        if link.is_symlink():
            if os.readlink(link) == target:
                return link  # already correct — idempotent re-registration
            link.unlink()
        elif link.exists():
            # The index is symlinks-only derived data; a stray regular
            # file here is never canonical — replace it.
            link.unlink()
        link.symlink_to(target)
        return link
    except Exception as exc:
        print(f"⚠️  specs index skipped: {exc}")
        return None


def repoint_run_dir_entries(
    old_dirname: str, new_dirname: str, new_label: str
) -> None:
    """Re-point index entries after a run-dir rename (the freeze's
    `runs/<slug>/` → `runs/<slug>--<name>/`). Fail-soft.

    Without this, every entry registered before the freeze dangles — its
    relative target still says `../runs/<slug>/…` — and because the rename
    changes `run_label` (slug → name), upsert's stale-cleanup (keyed on
    label+basename) never removes the old-label entry either, so the
    dangling link is permanent. Each affected entry is dropped and
    re-upserted against the renamed path under the run's new label, keeping
    the original registration date.
    """
    try:
        base = _work_repo_base()
        if base is None or old_dirname == new_dirname:
            return
        sdir = base / SPECS_DIR
        if not sdir.is_dir():
            return
        old_prefix = f"../runs/{old_dirname}/"
        for entry in list(sdir.iterdir()):
            if not entry.is_symlink():
                continue
            target = os.readlink(entry).replace(os.sep, "/")
            if not target.startswith(old_prefix):
                continue
            new_target = base / "runs" / new_dirname / target[len(old_prefix):]
            # Keep the entry's registration date and its basename. The
            # basename may be an alias (a masterplan run-root link name like
            # `mp-a-spec.md`) rather than the target's own filename, so it
            # is parsed out of the entry name (YYYY-MM-DD-<label>-<base>)
            # by stripping the label the entry was created under — the
            # pre-rename dir name (slug), or the run's name when it was set
            # before the freeze. Fallback: the target's filename.
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)$", entry.name)
            if not m:
                continue
            when = datetime.strptime(m.group(1), _DATE_PREFIX).replace(
                tzinfo=timezone.utc
            )
            rest = m.group(2)
            basename = None
            for candidate in (old_dirname, new_label):
                safe = _UNSAFE_LABEL_RE.sub("-", str(candidate)).strip("-")
                if safe and rest.startswith(f"{safe}-"):
                    basename = rest[len(safe) + 1:]
                    break
            if not basename:
                canonical_name = Path(target).name
                basename = (
                    canonical_name
                    if rest.endswith(f"-{canonical_name}")
                    else rest
                )
            entry.unlink()
            upsert_spec_link(new_target, new_label, when=when, alias=basename)
    except Exception as exc:
        print(f"⚠️  specs index re-point skipped: {exc}")


def list_entries() -> list[Path]:
    """The index's symlink entries, sorted by name (i.e. by date)."""
    sdir = specs_dir()
    if sdir is None or not sdir.is_dir():
        return []
    try:
        return sorted(p for p in sdir.iterdir() if p.is_symlink())
    except OSError:
        return []


def _registration_when(rdir: Path, name: str, canonical: Path) -> Optional[datetime]:
    """Best-effort registration time for a backfilled entry: the first
    `artifact_written` event for `name`, else the canonical file's mtime
    (fail-soft to None → "today", the least-wrong remaining answer)."""
    from . import run_state  # local: run_state imports this module
    try:
        for event in run_state.read_events(rdir, last_n=0):
            if event.get("type") == "artifact_written" and event.get("note") == name:
                return datetime.strptime(event["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(canonical.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def rebuild() -> list[Path]:
    """Rebuild the whole index from runs/ (backfill path, `work specs-index
    --rebuild`): drop every existing symlink entry, then re-index each
    run's spec-class artifacts — names registered in state.artifacts plus
    spec-class files/symlinks at the run-dir root (which covers masterplan
    run-root links; those resolve to their canonical bundle file). Entry
    dates come from the run's `artifact_written` events when recorded,
    else the file's mtime. Fail-soft per run and per entry."""
    from . import run_state  # local: run_state imports this module
    base = _work_repo_base()
    if base is None:
        return []
    sdir = base / SPECS_DIR
    if sdir.is_dir():
        for entry in list_entries():
            try:
                entry.unlink()
            except OSError as exc:
                print(f"⚠️  specs index: could not remove {entry.name}: {exc}")
    created: list[Path] = []
    runs = base / "runs"
    try:
        run_dirs = sorted(p for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else []
    except OSError:
        run_dirs = []
    for rdir in run_dirs:
        if rdir.name.startswith(".") or rdir.name == run_state.ARCHIVE_DIR:
            continue  # `.new-*` orphans and archived runs stay unindexed
        try:
            state = run_state._read_sibling_state(rdir)
            label = run_label(rdir, state)
            names = {str(v) for v in ((state or {}).get("artifacts") or {}).values()}
            names |= {p.name for p in rdir.iterdir() if not p.is_dir()}
            for name in sorted(n for n in names if is_spec_artifact(n)):
                path = rdir / name
                if not path.is_file():
                    continue
                # Canonical target only: a run-root convenience symlink
                # (masterplan sync) is followed to the bundle file, while
                # the run-root name stays the entry basename — matching
                # what the sync itself would have created.
                canonical = path.resolve() if path.is_symlink() else path
                link = upsert_spec_link(
                    canonical, label,
                    when=_registration_when(rdir, name, canonical),
                    alias=name,
                )
                if link is not None:
                    created.append(link)
        except Exception as exc:
            print(f"⚠️  specs index: skipped {rdir.name}: {exc}")
    return created
