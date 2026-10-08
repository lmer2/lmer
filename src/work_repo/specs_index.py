"""Central specs index (issue #101), form-routed (issue #353).

Specs land deep inside per-record dirs — `runs/<slug>/spec.md`, masterplan
bundle files at `runs/<slug>/masterplan/<mp>/spec.md` — so finding "that
spec from last week" means archaeology. The specs index is a flat
directory (sibling of the info and runs areas) of dated RELATIVE symlinks
pointing at each spec's canonical location: a plain directory listing IS
the index, so no README/index file is maintained beside the links (v1 is
symlinks-only by design).

The area is form-routed (D20): `{host}/{project}/specs/` in a legacy tree,
`<project>/_specs/` under the registry-resolved project dir in a
schema-form one. This module never re-derives that mapping —
:func:`specs_dir` asks `utils.project_area`, the one module that knows the
second set of names, and :func:`specs_rel_path` makes THAT dir relative to
the work-repo root, so the path staged for commit is always the path
written (it used to be a hand-built `{host}/{project}/specs` f-string,
which in schema form names a directory nothing writes).

Entry shape: `YYYY-MM-DD-<run>--<basename>` — the registration date, the
run's name (state.name, slug fallback) to disambiguate, then the spec's
own filename after a RESERVED `--` separator. The separator keeps
distinct (run, spec) pairs distinct — with a single `-`, (`a`,
`b-spec.md`) and (`a-b`, `spec.md`) would collide — and to keep the
first post-date `--` unambiguous the run-label component itself never
contains one: runs of hyphens in a label are collapsed to a single `-`
(dir-name fallback labels can be `slug--name` shaped).
Entry form: relative symlink to the one canonical file — never a copy
(the charter's "never write the same file in two places" rule, #103) and,
for masterplan bundle specs, never the run-root convenience symlink (the
bundle file is the single target). The target reaches into the RECORD dir,
which in schema form is one level deeper than in a legacy tree because the
session is the unit of record (D22): `../_runs/<slug>/<session-id>/…`,
including the `--<name>` suffix a freeze leaves on the session dir, rather
than `../runs/<dir>/…`. Relative, resolving and inside the plexus root are
ERROR-tier shape rules in every form (`index.link-not-relative`,
`index.link-escapes`, `index.dangling-link` in docs/SCHEMA.md), so both
ends of every target this module writes are resolved paths under the
project dir and the link between them is `os.path.relpath`'s answer.
One entry per (run, spec file): re-registering the same spec re-points
the same day's link, and a later-day re-registration replaces the older
dated entry rather than accumulating duplicates.

Spec-class predicate (is_spec_artifact): a Markdown file whose stem is
exactly `spec`, starts with `spec` + separator, or ends with separator +
`spec` (separators: `-`, `_`, `.`; case-insensitive). So `spec.md`,
`spec-auth.md`, `foo-spec.md`, `masterplan-spec.md` qualify; `plan.md`,
`spec.py` (not Markdown), `specifics.md`, `inspect.md` do not.

Every write path here is fail-soft after acquiring the checkout transaction:
an inherited Git hook is refused before lookup or mutation. The index is a
convenience view over the run area, and no index problem may ever fail the
artifact registration (or sync) that triggered it. `rebuild()` is the
backfill path for runs that predate the index; it asks the run-state
kernel's shared record walk (`run_state.iter_record_dirs`) which
directories are records, so the index and the resolvers can never meet
different sets.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .utils import _work_repo_base, project_area, work_repo_root
from .transactions import mutation_transaction

__all__ = [
    "SPECS_DIR", "SCHEMA_SPECS_DIR", "is_spec_artifact", "specs_dir",
    "specs_rel_path", "run_label", "upsert_spec_link", "list_entries",
    "rebuild", "repoint_run_dir_entries",
]

#: The area's two names, passed to `utils.project_area` (which owns the
#: legacy→schema mapping) and never joined onto a base by hand here.
SPECS_DIR = "specs"
SCHEMA_SPECS_DIR = "_specs"

# Spec-class filename stems (see module docstring for the rationale).
_SPEC_STEM_RE = re.compile(r"^spec$|^spec[-_.]|[-_.]spec$", re.IGNORECASE)
# Entry names this module owns: date prefix + label + `--` + basename.
_DATE_PREFIX = "%Y-%m-%d"
# Run labels come from state.name/slug (already slug-shaped); anything
# else is squashed so an entry name is always a plain filename.
_UNSAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")
# `--` is the reserved label/basename separator — labels never contain it
# (see _safe_label), so the first post-date `--` splits unambiguously.
_HYPHEN_RUN_RE = re.compile(r"-{2,}")


def is_spec_artifact(name: str) -> bool:
    """True when `name` is a spec-class artifact filename (see module doc)."""
    p = Path(str(name))
    return p.suffix.lower() == ".md" and bool(_SPEC_STEM_RE.search(p.stem))


def specs_dir() -> Optional[Path]:
    """The specs index area of this session's project dir: `<project>/specs`
    in a legacy tree, `<project>/_specs` in a schema-form one (D20).

    Routed through `utils.project_area`, which owns the legacy→schema name
    mapping and the project dir it hangs off — in schema form that dir is
    whatever the registry resolved (a short name, or a host bucket for a repo
    nobody has named yet), not a `{host}/{project}` path. None when nothing
    resolves, including the fail-closed case of a present-but-unusable
    manifest: an index written at the legacy names inside a schema tree is
    the split tree the validator flags.
    """
    return project_area(SPECS_DIR, SCHEMA_SPECS_DIR)


def specs_rel_path() -> Optional[str]:
    """The specs dir relative to the work-repo root, for commit staging.

    Derived from the RESOLVED area rather than re-composed from the env trio
    (issue #353): this used to be a hand-built `{host}/{project}/specs`
    f-string, the same bypass the run area's staging path shed, and in schema
    form it names a directory nothing writes — so `work commit` staged an
    empty path while the entries it had just created sat untracked. The two
    must address one directory, and only the resolver can reach a registry
    short name or a host bucket.

    None when no area resolves, and also when the resolved area lies outside
    the work repo — staging nothing rather than a path a commit would take
    literally (the rule :func:`run_state.run_rel_for_dir_name` follows).
    """
    sdir = specs_dir()
    if sdir is None:
        return None
    try:
        return sdir.relative_to(work_repo_root()).as_posix()
    except ValueError:
        return None


def run_label(rdir: Path, state: Optional[dict] = None) -> str:
    """The label an entry is named under: the RECORD's identity, not the
    run's (issue #353).

    The run identity comes first — state.name, slug fallback, then the
    directory name when no state is readable, since an unreadable sibling
    must never break indexing — and in schema form the session id is
    appended to it.

    That suffix is the whole point. In schema form the SESSION is the unit
    of record (D22): one `_runs/<slug>/` group holds several complete,
    independent records, and two of its launches share `slug` exactly and
    `name` whenever nobody named them differently. Keyed on the run alone,
    the second launch's :func:`upsert_spec_link` re-pointed the first
    launch's entry (same day) or unlinked it as stale (any other day), and
    that record's spec left the index with nothing raised and nothing
    logged. The session id is the one component nobody chooses, so it is
    what makes the key unique by construction rather than by luck.

    The question asked is the RECORD's shape (`run_state._is_session_record`)
    and deliberately not the TREE's form (`run_state.is_session_form`): a
    record sitting directly in the run area — a legacy tree, or a schema one
    adopted before its runs were migrated — is its target's only record and
    its directory name carries no session id, so the form question would
    have composed a useless `slug-slug` for it. A legacy tree's entry names
    are therefore byte-identical to what they always were.

    The dir-name fallback is returned unadorned: `<sid>` / `<sid>--<name>`
    already names one record.

    The cost, deliberate: entry names get long —
    `2026-08-25-develop-issue-101-20260824-101500-1-11111--spec.md` where the
    old name was `2026-08-25-develop-issue-101--spec.md`. A long name that
    names one record beats a short one that hides another.
    """
    from . import run_state  # local: run_state imports this module
    if state is None:
        state = run_state._read_sibling_state(rdir)
    label = None
    if isinstance(state, dict):
        if run_state.is_default_label(state):
            # A default label is not a naming (D6): the entry is named under
            # the slug, exactly as an unnamed record always was.
            recorded = state.get("slug")
        else:
            recorded = state.get("name") or state.get("slug")
        if recorded:
            label = str(recorded)
    if label is None:
        return rdir.name
    if not run_state._is_session_record(rdir):
        return label
    return _record_label(label, run_state.session_id_of(rdir.name))


def _record_label(label: str, session_id: str) -> str:
    """One spelling of `<run label>-<session id>`, shared by the code that
    CREATES an entry name and the code that RECOGNISES one.

    Two spellings of this would be two rules that drift, and the re-point
    below only finds an entry by rebuilding the label it was created under —
    so a mismatch there does not fail loudly, it silently re-registers the
    spec under a mangled basename.
    """
    return f"{label}-{session_id}" if session_id else label


def _record_session_id(area_rel_name: str) -> Optional[str]:
    """The session id carried by a RUN-AREA-RELATIVE record name, or None.

    `<slug>/<session-id>` in schema form, a bare `<dir>` in a legacy tree
    (which has no session level, hence None). Derived from the NAME rather
    than from the directory because the re-point's whole problem is that the
    old directory is gone by the time it runs — the name is all that is left
    to spell it with.
    """
    from . import run_state  # local: run_state imports this module
    parts = [part for part in str(area_rel_name).split("/") if part]
    if len(parts) < 2:
        return None
    return run_state.session_id_of(parts[-1])


def _safe_label(label: str) -> str:
    """The label component of an entry name: unsafe chars squashed AND runs
    of hyphens collapsed, so a label can never contain the reserved `--`
    separator (dir-name fallback labels are `slug--name` shaped) and the
    first `--` after the date is always the label/basename boundary."""
    return _HYPHEN_RUN_RE.sub("-", _UNSAFE_LABEL_RE.sub("-", label)).strip("-") or "run"


def _entry_name(label: str, basename: str, when: Optional[datetime]) -> str:
    when = when or datetime.now(timezone.utc)
    return f"{when.strftime(_DATE_PREFIX)}-{_safe_label(label)}--{basename}"


@mutation_transaction
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
    in place, and stale entries for the same pair under other dates —
    including ones in the legacy `<date>-<label>-<basename>` format that
    predates the reserved `--` separator — are removed, so
    re-registration never accumulates duplicates. The label is the RECORD's
    (:func:`run_label`), so in schema form that pair is one launch's spec
    and never a sibling launch's.

    An entry written under the OLD run-keyed label is left alone rather than
    swept. Re-deriving every name in one pass is `--rebuild`'s job; a freeze
    or re-slug rewrites such an entry in passing (the bare run label is still
    one of the labels the re-point recognises); and no work repo in the field
    has schema-form entries under the old key, so there is nothing to sweep
    in practice — only a cost to pay if this function started guessing.

    Returns the entry path, or None when nothing was indexed.
    """
    try:
        base = _work_repo_base()
        basename = alias or spec_path.name
        if base is None or not is_spec_artifact(basename):
            return None
        spec_path = spec_path if spec_path.is_absolute() else spec_path.absolute()
        # Inside the project dir in either form, which is what makes the
        # relative target below resolve and stay inside the plexus root: both
        # ends are under `base`, so `relpath` can only walk up as far as it.
        if not spec_path.is_file() or not spec_path.is_relative_to(base):
            return None
        sdir = specs_dir()
        if sdir is None:
            return None
        sdir.mkdir(parents=True, exist_ok=True)
        entry = _entry_name(label, basename, when)
        # One entry per (record, spec file): drop same-pair entries under
        # other dates before (re-)pointing today's. Same-pair entries in
        # the legacy pre-`--` name format are cleaned up too — but only
        # when the legacy label is itself `--`-free, since a `--`-bearing
        # legacy pattern could match a current-format entry of a DIFFERENT
        # pair (`--rebuild` regenerates whatever this skips).
        alternatives = [re.escape(entry[len("YYYY-MM-DD-"):])]
        legacy_label = _UNSAFE_LABEL_RE.sub("-", label).strip("-") or "run"
        if "--" not in legacy_label:
            alternatives.append(re.escape(f"{legacy_label}-{basename}"))
        stale_re = re.compile(
            rf"^\d{{4}}-\d{{2}}-\d{{2}}-(?:{'|'.join(alternatives)})$"
        )
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


@mutation_transaction
def repoint_run_dir_entries(
    old_name: str, new_name: str, new_label: str
) -> None:
    """Re-point index entries after a record dir moved (the freeze's
    `<slug>/` → `<slug>--<name>/`, the re-slug's move to a new address).
    Fail-soft.

    Without this, every entry registered before the move dangles — its
    relative target still names the old dir — and because a freeze changes
    `run_label` (slug → name), upsert's stale-cleanup (keyed on
    label+basename) never removes the old-label entry either, so the
    dangling link is permanent. Each affected entry is dropped and
    re-upserted against the moved path under the run's new label, keeping
    the original registration date.

    The interface stays NAME-keyed and the name carries the group. Both
    movers hand RUN-AREA-relative names (`run_state._area_rel_name`): a bare
    dir name in a legacy tree, `<slug>/<session-id>` in schema form, where
    the record is one level below the run area (D22) and a freeze renames the
    session while a re-slug moves it to another group. A path-keyed interface
    would work equally well for the target but not for the SOURCE — the old
    dir no longer exists by the time this runs — so the name is what both
    ends can still spell, and the one prefix rewrite below covers both forms
    because the run area itself is resolved rather than spelled.
    """
    try:
        from . import run_state  # local: run_state imports this module
        sdir = specs_dir()
        runs = run_state.runs_base()
        if sdir is None or runs is None or old_name == new_name:
            return
        if not sdir.is_dir():
            return
        # `../runs/` legacy, `../_runs/` schema — asked of the two resolved
        # areas rather than spelled, so the prefix written by upsert (which
        # is `relpath` between the same two dirs) and the prefix matched here
        # cannot drift apart.
        area_prefix = os.path.relpath(runs, sdir).replace(os.sep, "/")
        old_prefix = f"{area_prefix}/{old_name}/"
        for entry in list(sdir.iterdir()):
            if not entry.is_symlink():
                continue
            target = os.readlink(entry).replace(os.sep, "/")
            if not target.startswith(old_prefix):
                continue
            new_target = runs / new_name / target[len(old_prefix):]
            # Keep the entry's registration date and its basename. The
            # basename may be an alias (a masterplan run-root link name like
            # `mp-a-spec.md`) rather than the target's own filename, so it
            # is parsed out of the entry name (`YYYY-MM-DD-<label>--<base>`;
            # pre-`--` entries used a single dash) by stripping the label
            # the entry was created under — a component of the pre-move name
            # (the slug, or the session dir), or the run's name when it was
            # set before the freeze.
            # Fallback: the target's filename.
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)$", entry.name)
            if not m:
                continue
            when = datetime.strptime(m.group(1), _DATE_PREFIX).replace(
                tzinfo=timezone.utc
            )
            rest = m.group(2)
            basename = None
            # _safe_label collapses hyphen runs, so in a new-format entry the
            # first post-date `--` is always the label/basename boundary.
            # Sanitize candidates with the SAME helper the entry was created
            # with, and verify the label half against them BEFORE any legacy
            # single-dash stripping — a candidate that is a dash-prefix of
            # the created label (or a legacy basename containing `--`) must
            # not mis-split the name.
            # The COMPONENTS of the old name, not the whole string: an entry
            # is created under the run's label, which in schema form is the
            # group's slug or the session dir's own name — never the
            # `<slug>/<session-id>` pair. In a legacy tree the name has one
            # component, so this is the same list it always was.
            # BOTH spellings per component, the record label first. An
            # entry created under `<slug>-<sid>` is not recognised by the
            # bare `<slug>`: the `--` split below would reject it, the
            # legacy single-dash strip would then match `<slug>` and shear
            # the name mid-session-id, and the spec would be re-registered
            # under a basename like `20260824-101500-1-11111--spec.md`. The
            # bare spelling stays because a pre-#353 entry, an unmigrated
            # record and the dir-name fallback all carry it. `new_label`
            # arrives from `run_label` already record-keyed, so it is never
            # composed twice. In a legacy tree `record_id` is None and this
            # list is byte-identical to what it always was.
            components = [c for c in old_name.split("/") if c]
            record_id = _record_session_id(old_name)
            candidates: list[str] = []
            for component in components:
                if record_id and component != components[-1]:
                    candidates.append(_record_label(component, record_id))
                candidates.append(component)
            candidates.append(new_label)
            safe_candidates = [
                _safe_label(str(c)) for c in candidates if str(c)
            ]
            if "--" in rest:
                label_part, _, base_part = rest.partition("--")
                if label_part in safe_candidates and base_part:
                    basename = base_part
            if not basename:
                # Legacy entries created before `--` existed: `<label>-<base>`.
                for safe in safe_candidates:
                    if rest.startswith(f"{safe}-") and not rest.startswith(f"{safe}--"):
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


@mutation_transaction
def rebuild() -> list[Path]:
    """Rebuild the whole index from the run area (backfill path, `work
    specs-index --rebuild`): drop every existing symlink entry, then re-index
    each record's spec-class artifacts — names registered in state.artifacts
    plus spec-class files/symlinks at the record root (which covers masterplan
    run-root links; those resolve to their canonical bundle file). Entry
    dates come from the record's `artifact_written` events when recorded,
    else the file's mtime. Fail-soft per record and per entry.

    WHICH dirs are records is the run-state kernel's question, not this
    module's: `run_state.iter_record_dirs()` is the shared walk the resolvers
    and the session listing already use, and asking it is what makes the
    index reach one level deeper in schema form — each `_runs/<slug>/`
    grouping dir's session children, with the grouping level itself skipped,
    since nothing is ever written there (D22). It also carries the `.new-*`
    and `archive/` skips, so those stay unindexed as before.

    One consequence, deliberate: the walk reads "no state file" as "not a
    record", so a directory sitting directly under a legacy `runs/` with a
    spec and no state.yaml is no longer indexed (it was, under its own dir
    name). That rule is forced in schema form — such a directory IS the
    grouping level — and a second, form-dependent rule here is exactly the
    drift the shared walk exists to prevent.
    """
    from . import run_state  # local: run_state imports this module
    sdir = specs_dir()
    if sdir is None:
        return []
    if sdir.is_dir():
        for entry in list_entries():
            try:
                entry.unlink()
            except OSError as exc:
                print(f"⚠️  specs index: could not remove {entry.name}: {exc}")
    created: list[Path] = []
    for rdir in run_state.iter_record_dirs():
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
            # Named the way the movers name a record — run-area relative, so
            # a warning about one session dir says which run it belongs to.
            label = run_state.run_area_rel_name(rdir) or rdir.name
            print(f"⚠️  specs index: skipped {label}: {exc}")
    return created
