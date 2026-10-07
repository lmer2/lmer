"""`work adopt <host>/<project> <shortname> [--apply] [--compat-link]
[--description TEXT] [--remote URL]... [--force]` — name a host-bucket tree
and register it (issue #367; run-identity spec §13.B, §1.6, §10).

What it does
============
Spec D9: names are never derived. An unregistered repo's work sits in its
host bucket (`{host}/{project}/`, SCHEMA.md "Host buckets") until a human
names it, and adoption is the tooling job that names it: ONE commit that
adds the registry entry and moves the directory to `<short-name>/`. Dry run
by default; the apply is `--apply`.

1. RESOLVE — `<host>/<project>` must be a REAL directory (never a symlink)
   under the work-repo root that the ONE project walk
   (:func:`lmer_schema.checks.walk.project_dirs`) lists as a host bucket: a
   dotted top-level name and the §1 marker test, at whatever depth the
   project-shaped directory sits. The entry's `repo:` is the bucket path
   through the one normalizer (D21) — the bucket path already spells
   `host/path` — and each `--remote` is an additional `remotes:` entry, in
   canonical form too.
2. CHECK, before anything moves: the shortname charset; no registry entry
   and no top-level directory already using the name; no remote bound to
   another project (the bucket's OWN remote bound elsewhere is the §4 split
   tree and is named as such); `schema:` writable, which is
   :func:`lmer_schema.writer.load_for_write`'s refusal verbatim — a newer
   schema, a symlinked or unparseable file, a registry that does not build;
   the registry's own rules over the would-be entry (a hand-built
   :class:`~lmer_schema.manifest.Manifest` through
   :func:`~lmer_schema.registry.build_registry`, no write, so the dry run
   refuses exactly what the apply would); the involved paths clean in git.
   Then the SCHEMA-tier fix list (below).
3. BOOTSTRAP — a tree with no manifest is a legacy tree, and a present
   manifest is what makes it schema form (D20). The apply creates the
   manifest only through :func:`lmer_schema.writer.create` (never as a side
   effect of a mutation), with the instance `name:` taken from the
   checkout's directory name, and the ONE-line notice that this flips the
   whole tree is printed by the dry run too: the first adoption in an
   instance is a bigger step than the tenth.
4. APPLY — under the mutation lock: the clean-tree re-check, the §1.6
   preflight (:func:`work_repo.migrate.preflight_no_live_records` over
   every record under the bucket; any line refuses with nothing changed),
   the registry entry through the writer FIRST (the step that can still be
   refused goes before the step that moves anything; a bootstrap whose
   registration is refused is unlinked again) and staged, `git mv
   <host>/<project> <shortname>` (a plain rename only when git tracks nothing under it — an
   empty area, which git cannot move), with `--compat-link` a relative,
   depth-matched symlink at the old path (the §4 "symlink = INFO" form),
   the namespace levels the move emptied removed, and ONE commit through
   :func:`git_ops.commit_work_path` — the existing commit → rebase → push
   helper — with both paths staged so the move lands as a move.
5. RECORD — inside a session, one :data:`ADOPT_EVENT` (name + repo) and a
   run-log line in the CALLER's own record, staged with the commit;
   outside, only the commit, whose message carries the same facts. Adopt is
   not an execution writing a record (§1.6): it never calls `write_state`,
   `append_event` or `freeze_run_dir` on a record it moves, mints no
   identity for history, and dumps no YAML of its own — the manifest is the
   writer's, the record is run_state's (`tests/test_work_repo_adopt.py`
   guards the source).

The schema-tier fix list (D14)
==============================
The bucket is leaving the lenient tier: once it is `<short-name>/` its
contents are checked at the schema tier, so what that tier would raise is
shown NOW. The runner pins a bucket's contents to the legacy tier
(:mod:`lmer_schema.checks.runner`, "The bucket tier") and no check module
can be asked to walk one bucket at another tier, so the list is computed
from the runner's own report: :func:`lmer_schema.checks.runner.validate`
over the whole tree, the findings at or under the bucket path, each
re-resolved through the code table (:data:`lmer_schema.checks.findings.CHECKS`,
the D14 tier map) at :attr:`Form.SCHEMA`. One source of findings and one
severity table, no second walk and no restated rule. The layout's listing
of the bucket itself (`tree.host-bucket`, `tree.legacy-project-home`)
describes the status the adoption changes, not the contents, and is left
out. An ERROR refuses the apply unless `--force` is given; WARN and INFO
never block (D14).

What adopt does NOT do: rename the bare areas inside the tree (`runs/` →
`_runs/` and the rest). That is `work migrate <short-name>`; after adoption
the tree is registered and flat-named but still shaped as it was, and the
validator's output is the migrate to-do list.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import git_ops, run_state
from .migrate import preflight_no_live_records
from .transactions import work_mutation
from .utils import MANIFEST_NAME, CoreUnavailableError, core_module, work_repo_root

__all__ = [
    "ADOPT_EVENT",
    "AdoptError",
    "Plan",
    "adopt_project",
    "cmd_adopt",
    "plan_adoption",
    "schema_tier_fix_list",
    "write_rel_paths",
]

#: The event one applied adoption leaves in the CALLER's record.
ADOPT_EVENT = "project_adopted"

#: The layout check's listing of the bucket itself — the status the adoption
#: changes, not a finding about the contents — left out of the fix list.
_BUCKET_LISTING_CODES = frozenset({"tree.host-bucket", "tree.legacy-project-home"})

#: The instance `name:` a bootstrap writes when the checkout has no usable
#: directory name (a root mounted at `/`).
_FALLBACK_INSTANCE = "work"


class AdoptError(Exception):
    """A precondition the command refuses on, with the remedy in the text."""


@dataclass(frozen=True)
class Plan:
    """What `work adopt <host>/<project> <shortname>` would do, as data.

    `bucket` is the work-repo-relative bucket path (the directory that
    moves), `name` the short name it moves to, `repo`/`remotes` the
    canonical remotes the entry registers, `bootstrap` whether the apply
    creates the manifest first (and `schema`/`instance` what it writes),
    `tracked` whether git tracks anything under the bucket (a `git mv` or a
    plain rename), `link` the relative symlink target left at the old path
    with `--compat-link`, `fix_list` the schema-tier findings, `records` the
    record dirs that move (the preflight's input). Git cleanliness is not
    stored here — the dry run reports it and the apply re-checks it under
    the lock, since it can change in between.
    """

    root: Path
    bucket: str
    name: str
    repo: str
    remotes: tuple
    description: Optional[str]
    bootstrap: bool
    schema: int
    instance: str
    tracked: bool
    link: Optional[str]
    fix_list: tuple
    records: tuple
    force: bool

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def bucket_dir(self) -> Path:
        return self.root / self.bucket

    def errors(self) -> tuple:
        """The fix-list findings that are ERRORs at the schema tier."""
        return tuple(f for f in self.fix_list if f.severity.value == "ERROR")

    def blockers(self) -> tuple:
        """What stops the apply: schema-tier errors, unless forced."""
        errors = self.errors()
        if errors and not self.force:
            return (
                f"{_count(len(errors), 'schema-tier error')} in the fix list above; "
                "fix them, or pass --force to adopt anyway",
            )
        return ()

    def git_paths(self) -> list:
        """Every work-repo-relative path the apply touches, for the
        clean-tree check and for staging: the manifest, both ends of the
        move."""
        return [MANIFEST_NAME, self.bucket, self.name]

    def cleanups(self) -> list:
        """The namespace levels above the bucket that the move leaves empty
        (git tracks none), innermost first — `rmdir`'d by the apply so no
        bare host shell survives the adoption. With a compat link the levels
        hold the link and stay."""
        if self.link:
            return []
        out = []
        removed = self.bucket
        parent = os.path.dirname(removed)
        while parent:
            directory = self.root / parent
            if directory.is_symlink() or not directory.is_dir():
                break
            try:
                left = [c for c in directory.iterdir() if c.name != os.path.basename(removed)]
            except OSError:
                break
            if left:
                break
            out.append(parent)
            removed, parent = parent, os.path.dirname(parent)
        return out

    def move_line(self) -> str:
        verb = "git mv" if self.tracked else "mv"
        return f"{verb} {self.bucket} {self.name}"

    def link_line(self) -> str:
        return f"ln -s {self.link} {self.bucket}"

    def entry_lines(self) -> list:
        """The registry entry as the dry run shows it. Display only: the
        manifest is written by the writer, never from these lines."""
        lines = [f"repo: {self.repo}"]
        if self.remotes:
            lines.append("remotes:")
            lines.extend(f"  - {remote}" for remote in self.remotes)
        if self.description is not None:
            lines.append(f"description: {self.description}")
        return lines

    def notice(self) -> Optional[str]:
        """The one-line bootstrap notice (D20), or None when a manifest is
        already there."""
        if not self.bootstrap:
            return None
        return (
            f"⚠️  BOOTSTRAP: no {MANIFEST_NAME} at {self.root} — the apply CREATES one "
            f"(schema: {self.schema}, name: {self.instance}) and that flips the WHOLE "
            "tree to schema form (D20): every `{host}/{project}` home becomes a host "
            "bucket and nothing in this tree is read at the lenient legacy tier again."
        )

    def fix_list_lines(self) -> list:
        head = (
            f"schema-tier fix list for `{self.bucket}/` "
            f"(what the validator raises once it is `{self.name}/`)"
        )
        if not self.fix_list:
            return [f"{head}: clean"]
        lines = [
            f"{head}: {_count(len(self.fix_list), 'finding')}, "
            f"{_count(len(self.errors()), 'error')}"
        ]
        for finding in self.fix_list:
            lines.append(f"  {finding.severity.value:<5}  {finding.code}  {finding.path}")
            lines.append(f"         {finding.message}")
            if finding.detail:
                lines.append(f"         detail: {finding.detail}")
        return lines

    def render(self) -> str:
        """The dry-run text: every planned action, the fix list, the blockers."""
        lines = [f"work adopt {self.name} — plan for `{self.bucket}/`"]
        notice = self.notice()
        if notice:
            lines.append(notice)
        lines.append(f"{MANIFEST_NAME}: add projects.{self.name}")
        lines.extend(f"    {line}" for line in self.entry_lines())
        lines.append(self.move_line())
        if self.link:
            lines.append(self.link_line())
        lines.extend(f"rmdir {rel}" for rel in self.cleanups())
        lines.append("commit message:")
        lines.extend(f"    {line}" for line in self.commit_message().rstrip("\n").splitlines())
        lines.append(
            f"records under the bucket: {len(self.records)} — they move with it; "
            "the apply refuses while any is live (§1.6)"
        )
        lines.extend(self.fix_list_lines())
        blockers = self.blockers()
        if blockers:
            lines.append("blockers (the apply refuses until these are resolved):")
            lines.extend(f"  - {blocker}" for blocker in blockers)
        elif self.errors():
            lines.append(
                f"--force: {_count(len(self.errors()), 'schema-tier error')} carried "
                f"into `{self.name}/` as is"
            )
        lines.append("dry run — add --apply to perform this in ONE commit")
        return "\n".join(lines)

    def commit_message(self) -> str:
        """ONE commit, a fixed shape: the subject names the move and the
        remote, the body lists every action — the durable trail whether or
        not a session record carried the event."""
        subject = f"work adopt {self.name}: {self.bucket} -> {self.name}/ (repo {self.repo})"
        body = []
        if self.bootstrap:
            body.append(
                f"{MANIFEST_NAME}: created (schema: {self.schema}, name: {self.instance}) — "
                "the tree is schema form from this commit (D20)"
            )
        body.append(f"{MANIFEST_NAME}: projects.{self.name}.repo: {self.repo}")
        if self.remotes:
            body.append(
                f"{MANIFEST_NAME}: projects.{self.name}.remotes: {', '.join(self.remotes)}"
            )
        if self.description is not None:
            body.append(f"{MANIFEST_NAME}: projects.{self.name}.description: {self.description}")
        body.append(self.move_line())
        if self.link:
            body.append(self.link_line())
        body.extend(f"rmdir {rel}" for rel in self.cleanups())
        return subject + "\n\n" + "\n".join(body) + "\n"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# ---------------------------------------------------------------------------
# The schema-tier fix list
# ---------------------------------------------------------------------------


def schema_tier_fix_list(root, bucket_rel: str) -> tuple:
    """The validator's findings at or under *bucket_rel*, each at the
    severity the SCHEMA tier gives its code (module docstring, "The
    schema-tier fix list"), in report order. The bucket's own listing is
    left out. Raises :class:`AdoptError` when the tree is unusable input to
    the validator (nothing below it could be judged)."""
    findings_api = core_module("checks.findings")
    CHECKS, Finding, Form = findings_api.CHECKS, findings_api.Finding, findings_api.Form
    validate = core_module("checks.runner").validate

    report = validate(root)
    if report.unusable is not None:
        raise AdoptError(f"the validator cannot read {root}: {report.unusable}")
    found = []
    for finding in report.findings:
        at_bucket = finding.path == bucket_rel
        if not (at_bucket or finding.path.startswith(f"{bucket_rel}/")):
            continue
        if at_bucket and finding.code in _BUCKET_LISTING_CODES:
            continue
        found.append(Finding(
            code=finding.code,
            severity=CHECKS.severity_for(finding.code, Form.SCHEMA),
            path=finding.path,
            message=finding.message,
            detail=finding.detail,
        ))
    return tuple(sorted(found, key=lambda finding: finding.sort_key))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_adoption(
    bucket,
    name,
    *,
    root: Optional[Path] = None,
    remotes: Iterable[str] = (),
    description: Optional[str] = None,
    compat_link: bool = False,
    force: bool = False,
) -> Plan:
    """The plan for adopting *bucket* as *name*, after every check that
    needs no lock (module docstring, steps 1–3). Raises :class:`AdoptError`
    on each refusal, with the remedy in the text."""
    root = Path(root) if root is not None else work_repo_root()
    try:
        from lmer_cli.container.sources import scrub_credentials
        Form = core_module("checks.findings").Form
        walk_api = core_module("checks.walk")
        BUCKET, LEGACY_HOME, project_dirs = walk_api.BUCKET, walk_api.LEGACY_HOME, walk_api.project_dirs
        manifest_api = core_module("manifest")
        Manifest, ManifestError = manifest_api.Manifest, manifest_api.ManifestError
        registry_api = core_module("registry")
        PROJECT_MARKERS, bucket_path, build_registry, is_short_name = (
            registry_api.PROJECT_MARKERS, registry_api.bucket_path,
            registry_api.build_registry, registry_api.is_short_name,
        )
        normalize = core_module("urls").normalize
        writer_api = core_module("writer")
        BOOTSTRAP_SCHEMA, load_for_write = writer_api.BOOTSTRAP_SCHEMA, writer_api.load_for_write
    except ImportError as exc:  # CoreUnavailableError is one
        raise AdoptError(
            "this lmer build has no lmer-schema core and is below the version "
            f"floor for a schema-form tree ({exc}); update lmer on this host first"
        ) from exc

    rel = _bucket_rel(bucket)
    if rel is None:
        raise AdoptError(
            f"`{bucket}` is not a host-bucket path: expected `<host>/<project>` "
            f"relative to {root}, the host a dotted top-level directory "
            "(SCHEMA.md 'Host buckets')"
        )
    path = root / rel
    if path.is_symlink():
        raise AdoptError(
            f"`{rel}` is a symlink -> {os.readlink(path)}, not a real directory; "
            "adopt the directory it points at"
        )
    if not path.is_dir():
        raise AdoptError(f"`{rel}` is not a directory under {root}")
    if not is_short_name(name):
        raise AdoptError(
            f"`{name}` is not a short name: names match `[a-z0-9][a-z0-9_-]*` "
            "(lowercase, no dots — a dot marks a host bucket — no slashes, no "
            "leading `_` or `-`)"
        )

    manifest_path = root / MANIFEST_NAME
    bootstrap = not (manifest_path.is_symlink() or manifest_path.exists())
    if bootstrap:
        manifest, registry, form = None, None, Form.LEGACY
        schema, instance = BOOTSTRAP_SCHEMA, _instance_name(root)
    else:
        # The writer's refusal IS the "schema: is writable" check — a newer
        # schema, a symlinked or unparseable file, a registry that does not
        # build. The handle is discarded; the apply opens its own.
        try:
            handle = load_for_write(manifest_path)
        except ManifestError as exc:
            raise AdoptError(f"`schema:` is not writable: {exc}") from exc
        manifest, form = handle.manifest, Form.SCHEMA
        registry = build_registry(manifest)
        schema, instance = manifest.schema, manifest.name
        if name in registry:
            raise AdoptError(
                f"`projects.{name}` already exists in {MANIFEST_NAME}; pick another "
                "short name"
            )
    if os.path.lexists(root / name):
        raise AdoptError(
            f"`{name}/` already exists at the top level of {root}; pick another "
            "short name, or merge the bucket into it by hand"
        )

    # The ONE project walk decides what is a bucket: a dotted top-level
    # name and the §1 marker test, at whatever depth the project sits.
    entry = next((e for e in project_dirs(root, registry=registry, form=form) if e.rel == rel), None)
    if entry is None or entry.kind not in (BUCKET, LEGACY_HOME):
        raise AdoptError(
            f"`{rel}` is not a host bucket's project directory: the §1 marker test "
            f"wants one of {', '.join(PROJECT_MARKERS)} directly inside it — a "
            "namespace level above the project, a directory below it, or a "
            "short-name directory at the root is not the bucket"
        )

    repo = normalize(_remote_spelling(rel, schema))
    if bucket_path(root, repo, schema=schema) != path:
        raise AdoptError(
            f"`{rel}` is not the bucket of the remote it spells ({repo} would be "
            f"parked at {bucket_path(root, repo, schema=schema)}); rename the "
            "directory to its canonical spelling first"
        )
    canonical = []
    for value in remotes:
        spelling = normalize(value) if isinstance(value, str) else ""
        if not spelling or "/" not in spelling:
            raise AdoptError(
                f"--remote {scrub_credentials(str(value))!r} is not a repository URL "
                "(expected `host/path`); a bare remote NAME like `origin` is not one"
            )
        canonical.append(spelling)
    if registry is not None:
        for spelling in (repo, *canonical):
            owner = registry.lookup(spelling)
            if owner is None:
                continue
            if spelling == repo:
                raise AdoptError(
                    f"`{rel}` is the bucket of a remote `projects.{owner.name}` already "
                    f"registers: a split tree (§4) — merge the directory into "
                    f"`{owner.name}/` by hand; there is nothing to adopt"
                )
            raise AdoptError(
                f"remote {spelling} is bound to `projects.{owner.name}`; a repo binds "
                "to at most one project"
            )

    # The registry's own rules over the would-be entry, with no write: the
    # dry run refuses exactly what the writer's validation would.
    doc = {"repo": repo}
    if canonical:
        doc["remotes"] = list(canonical)
    if description is not None:
        doc["description"] = description
    probe = Manifest(
        path=manifest_path, schema=schema, name=instance,
        projects={**(manifest.projects if manifest is not None else {}), name: doc},
        sources={}, housekeeping={}, warnings=(),
    )
    try:
        build_registry(probe)
    except ManifestError as exc:
        raise AdoptError(f"the registry refuses `projects.{name}`: {exc}") from exc

    link = os.path.relpath(name, start=os.path.dirname(rel)) if compat_link else None
    return Plan(
        root=root, bucket=rel, name=name, repo=repo, remotes=tuple(canonical),
        description=description, bootstrap=bootstrap, schema=schema, instance=instance,
        tracked=git_ops._has_tracked_files(root, rel), link=link,
        fix_list=schema_tier_fix_list(root, rel), records=tuple(_record_dirs(path)),
        force=force,
    )


def _bucket_rel(value) -> Optional[str]:
    """*value* as the work-repo-relative bucket path, or None when it is not
    one: relative, `<host>/<path...>` with plain components and a dotted
    host. Never raises (the journal seam calls it on raw arguments)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.startswith("/"):
        return None
    parts = text.rstrip("/").split("/")
    if len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
        return None
    try:
        is_host_bucket_name = core_module("registry").is_host_bucket_name
    except CoreUnavailableError:
        return None
    if not is_host_bucket_name(parts[0]):
        return None
    return "/".join(parts)


def _remote_spelling(rel: str, schema: int) -> str:
    """The bucket path as the remote it was parked for: schema 2 spells a
    non-default port into the host directory as `%3A<port>`
    (:func:`lmer_schema.registry.bucket_path`); the registry value carries
    the port as `host:port`. The round trip back through `bucket_path` is
    checked by the planner."""
    host, sep, tail = rel.partition("/")
    if schema >= 2:
        host = host.replace("%3A", ":")
    return host + sep + tail


def _instance_name(root: Path) -> str:
    """The `name:` a bootstrap writes: the checkout's directory name."""
    try:
        name = Path(root).resolve().name
    except OSError:
        name = Path(root).name
    return name.strip() or _FALLBACK_INSTANCE


def _record_dirs(bucket: Path) -> list:
    """Every record under *bucket* — any directory holding a state file, at
    any depth, in either shape (`_runs/<slug>/<session>/`, legacy
    `runs/<slug>/`, both archive homes), dot-dirs included: a seed in flight
    is a writer too. A record is never descended into (what is inside it is
    the run's own content); symlinked levels are never followed."""
    found = []
    for dirpath, dirnames, _files in os.walk(bucket):
        here = Path(dirpath)
        if here != bucket and run_state._state_path(here) is not None:
            found.append(here)
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink())
    return found


def _dirty_paths(root: Path, rels: list) -> list:
    """`git status --porcelain` over the paths that exist; any line is dirt.
    Untracked content counts: `git mv` would carry it along unstaged."""
    present = [rel for rel in rels if os.path.lexists(root / rel)]
    if not present:
        return []
    rc, output = git_ops.run_git_command(
        ["status", "--porcelain", "--", *present], root, check=False
    )
    if rc != 0:
        raise AdoptError(f"git status failed under {root}: {output}")
    return [line for line in output.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _apply(plan: Plan, out: Callable[[str], None], err: Callable[[str], None]) -> int:
    """Under the mutation lock: re-check the tree, run the preflight, write
    the registry entry, move, link, record, commit once. Returns the exit
    code."""
    ManifestError = core_module("manifest").ManifestError
    writer_api = core_module("writer")
    create, load_for_write = writer_api.create, writer_api.load_for_write

    root = plan.root
    configured = work_repo_root()
    if _resolved(root) != _resolved(configured):
        raise AdoptError(
            f"LMER_WORK_REPO_PATH ({configured}) must name the tree being adopted "
            f"into ({root}): the commit helper is bound to the configured checkout"
        )
    with work_mutation(root):
        problems = list(plan.blockers())
        if os.path.lexists(root / plan.name):
            problems.append(f"`{plan.name}/` now exists at the top level of {root}")
        if plan.bucket_dir.is_symlink() or not plan.bucket_dir.is_dir():
            problems.append(f"`{plan.bucket}` is no longer a real directory")
        problems += [f"dirty in git: {line}" for line in _dirty_paths(root, plan.git_paths())]
        problems += preflight_no_live_records(plan.records)
        if problems:
            for problem in problems:
                err(f"❌ {problem}")
            err("refused: nothing was changed")
            return 1

        # 1. The registry entry, through the writer. The step that can still
        # be refused goes first, so a refusal changes nothing; a bootstrap
        # whose registration is refused is taken back.
        try:
            if plan.bootstrap:
                out(plan.notice())
                writer = create(plan.manifest, plan.instance)
            else:
                writer = load_for_write(plan.manifest)
            try:
                writer.add_project(
                    plan.name, repo=plan.repo, remotes=plan.remotes,
                    description=plan.description,
                )
            except BaseException:
                if plan.bootstrap:
                    plan.manifest.unlink(missing_ok=True)
                raise
        except ManifestError as exc:
            err(f"❌ {MANIFEST_NAME}: {exc}")
            err("refused: nothing was changed")
            return 1
        rc, output = git_ops.run_git_command(["add", "--", MANIFEST_NAME], root, check=False)
        if rc != 0:
            err(f"❌ git add {MANIFEST_NAME} failed: {output}")
            err(
                f"{MANIFEST_NAME} already carries projects.{plan.name} and nothing "
                "is committed: inspect `git status` and finish or revert by hand"
            )
            return rc
        out(f"{MANIFEST_NAME}: projects.{plan.name} added")

        # 2. The move: `git mv` keeps history; a plain rename only when git
        # tracks nothing under the bucket (empty areas git cannot move).
        if plan.tracked:
            rc, output = git_ops.run_git_command(["mv", plan.bucket, plan.name], root, check=False)
            if rc != 0:
                err(f"❌ git mv {plan.bucket} {plan.name} failed: {output}")
                err(
                    f"{MANIFEST_NAME} already carries projects.{plan.name} and nothing "
                    "is committed: inspect `git status` and finish or revert by hand"
                )
                return rc
        else:
            os.rename(plan.bucket_dir, root / plan.name)
        out(plan.move_line())

        # 3. The compat link at the old path, staged with the move.
        if plan.link:
            os.symlink(plan.link, plan.bucket_dir)
            rc, output = git_ops.run_git_command(["add", "--", plan.bucket], root, check=False)
            if rc != 0:
                err(f"❌ git add {plan.bucket} failed: {output}")
                return rc
            out(plan.link_line())

        # 4. The namespace levels the move emptied.
        for rel in plan.cleanups():
            try:
                os.rmdir(root / rel)
                out(f"rmdir {rel}")
            except OSError as exc:
                out(f"⚠️  {rel} not removed: {exc}")

        # 5. The caller's record, then ONE commit with both paths staged.
        paths = plan.git_paths() + _record_in_caller(plan, out)
        return git_ops.commit_work_path(paths, plan.commit_message())


def _resolved(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def _caller_record() -> Optional[Path]:
    """The caller's own, EXISTING record — where the event and the run-log
    line go when the command runs inside a session; None from an operator
    shell (nothing resolves), never raised over."""
    try:
        own = run_state.run_dir()
    except Exception:
        return None
    if own is None or run_state._state_path(own) is None:
        return None
    return own


def _record_in_caller(plan: Plan, out: Callable[[str], None]) -> list:
    """One :data:`ADOPT_EVENT` plus a run-log line in the CALLER's own record
    when there is one; the paths to stage it with. From an operator shell
    there is nothing to write — the commit message carries the facts — and
    that is said, not assumed. Fail-soft: a record that refuses the event
    never fails an adoption that already happened."""
    caller = _caller_record()
    if caller is None:
        out("ℹ️  no session record here; the commit message records the adoption")
        return []
    try:
        run_state.append_event(
            caller, ADOPT_EVENT, note=f"{plan.bucket} -> {plan.name}",
            data={"name": plan.name, "repo": plan.repo, "from": plan.bucket, "to": plan.name},
        )
    except (run_state.RunStateError, OSError) as exc:
        out(f"⚠️  {ADOPT_EVENT} event not recorded in {caller}: {exc}")
    try:
        from .loggers import get_logger  # local: loggers imports run_state

        get_logger(caller / "log.yaml").log(
            f"work adopt {plan.name}: adopted {plan.bucket} as {plan.name}/ (repo {plan.repo})"
        )
    except Exception as exc:  # the log is a courtesy, never the result
        out(f"⚠️  run-log line not recorded in {caller}: {exc}")
    try:
        return list(run_state.run_rel_path_candidates())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def adopt_project(
    bucket,
    name,
    *,
    root: Optional[Path] = None,
    apply: bool = False,
    compat_link: bool = False,
    description: Optional[str] = None,
    remotes: Iterable[str] = (),
    force: bool = False,
    out: Callable[[str], None] = print,
    err: Optional[Callable[[str], None]] = None,
) -> int:
    """`work adopt <host>/<project> <shortname> [...]` as a library call.

    Plans (printing the plan through *out* on a dry run), applies when
    asked, and reports every refusal through *err* (stderr by default) with
    exit code 1.
    """
    if err is None:
        err = lambda line: print(line, file=sys.stderr)  # noqa: E731
    if isinstance(remotes, str):
        remotes = (remotes,)
    try:
        plan = plan_adoption(
            bucket, name, root=root, remotes=remotes, description=description,
            compat_link=compat_link, force=force,
        )
        if not apply:
            out(plan.render())
            dirty = _dirty_paths(plan.root, plan.git_paths())
            if dirty:
                out("dirty in git (the apply refuses until these are clean):")
                for line in dirty:
                    out(f"  {line}")
            return 0
        rc = _apply(plan, out, err)
    except AdoptError as exc:
        err(f"❌ {exc}")
        return 1
    if rc == 0:
        out(f"✅ work adopt {name}: `{plan.bucket}/` is `{name}/` and registered, in one commit")
    return rc


def cmd_adopt(args) -> int:
    """The `work adopt` handler. Reads `args.bucket` (`<host>/<project>`),
    `args.name` (the short name), `args.apply`, `args.compat_link`,
    `args.description`, `args.remote` (a list — `--remote` repeats) and
    `args.force`."""
    return adopt_project(
        getattr(args, "bucket", None),
        getattr(args, "name", None),
        apply=bool(getattr(args, "apply", False)),
        compat_link=bool(getattr(args, "compat_link", False)),
        description=getattr(args, "description", None),
        remotes=tuple(getattr(args, "remote", None) or ()),
        force=bool(getattr(args, "force", False)),
    )


def write_rel_paths(args) -> Optional[list]:
    """The work-repo-relative paths a `work adopt` invocation may write;
    None for a dry run (read-only). The write-attribution journal's scope
    (issue #233): the manifest, the bucket path and the short name — both
    ends of the move — plus the caller's own record when there is one. A
    malformed argument contributes nothing. Never raises."""
    if not getattr(args, "apply", False):
        return None
    rels = [MANIFEST_NAME]
    bucket = _bucket_rel(getattr(args, "bucket", None))
    if bucket is not None:
        rels.append(bucket)
    name = getattr(args, "name", None)
    try:
        is_short_name = core_module("registry").is_short_name
    except CoreUnavailableError:  # pragma: no cover - the apply refuses later anyway
        is_short_name = lambda value: False  # noqa: E731
    if is_short_name(name) and name not in rels:
        rels.append(name)
    try:
        rels.extend(rel for rel in run_state.run_rel_path_candidates() if rel not in rels)
    except Exception:
        pass
    return rels
