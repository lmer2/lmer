"""The composing validation core — one run over one plexus (spec §7, D14/D20;
SCHEMA.md "Validator").

This is the importable half of ``lmer-schema validate``: it resolves the
target path, detects the tree's form through the core API, and executes every
check module once, in one declared order, into one
:class:`~lmer_schema.checks.findings.Report`. It is the ONLY module that names
the check modules — a check module never imports a sibling — so the wiring,
the ordering and the short-circuit rules are readable in one file, and a new
check module joins the run by being added to :data:`CHECK_ORDER` (a test
fails if it is not).

The runner owns no check logic. It opens no file of the tree, parses nothing
and knows no schema path; every finding about the plexus comes from a check
module. Its own ``runner.*`` codes are about the RUN, not about the tree.

The entry-point convention
==========================
The six modules landed in parallel and their entry points differ — root-first
and report-first, ``registry=`` with a load-me sentinel, keyword-only extras,
three of them returning the artifact the next one needs. Rather than churn six
modules (and their tests) for cosmetics, the convention is imposed HERE:

    every check is reached through one adapter with one signature,
    ``_check_<module>(context) -> None``, which is the only place that knows
    that module's spelling.

:class:`Context` is the shared state those adapters read and write — the
loaded manifest, the built registry, the project-dir list — so the pipeline is
a straight line: what one stage produces, the next consumes. The convention
the modules themselves should converge on (a later cleanup, not this task) is
``check_<area>(root, report, *, ...)`` returning what it produced;
``registry.check_registry(report, manifest)`` is the one that does not fit it
today.

Order, and what each stage hands on
===================================
``manifest -> registry -> layout -> agents -> record -> index_links``.

The first three are a dependency chain: the manifest is loaded once and handed
to the registry check, the built registry is handed to the layout check (which
would otherwise re-read it from disk), and the layout walk's
:class:`~lmer_schema.checks.walk.ProjectDir` list is handed to the two
per-project modules, which would otherwise each re-walk the tree. **The tree
is walked once** — and by one shared implementation
(:mod:`lmer_schema.checks.walk`), so what the runner hands down is the same
answer each module would have discovered for itself (!256 discussion 5).

The last three are independent of each other and are ordered outside-in: the
agent surface, then the record, then the indexes that point into the record.

Short-circuit rules (the exit-2 decision)
=========================================
Three levels of "something is too broken to continue", deliberately distinct:

1. **No tree.** The target is not a directory: one ``runner.no-such-path``
   error, the report is unusable (exit 2) and NO check runs. There is nothing
   to say anything about.
2. **An unusable manifest.** A present-but-broken ``_plexus.yaml``, or one
   declaring a ``schema:`` int newer than this lmer understands, is unusable
   input (exit 2) — the manifest check marks it so. The runner then STOPS:
   the downstream checks do NOT run against an empty registry. Running them
   would report a tree nobody could read — every registered project dir
   becomes an "unregistered project" warning, every registered remote's
   ``{host}/{project}/`` path becomes a plain bucket instead of a split-tree
   error, and the operator gets a fix-list whose every line is an artefact of
   the one real defect. The skipped checks are announced
   (``runner.checks-skipped``, informational) so a short report is never
   mistaken for a clean tree. Fix the manifest, run again, and the list is
   trustworthy.
3. **A registry that does not build.** This is NOT exit 2. The manifest
   parsed, so the tree, its form and every non-registry surface still say
   something true; only the identity layer is unusable. The registry check
   turns the ``RegistryError`` into one ``registry.invalid`` error (exit 1)
   and returns ``None``, and the run continues **registry-free**: the layout
   check takes ``registry=None``, which is the same input a legacy tree gives
   it, so it runs its registry-free half (buckets, the ``_`` reservation, the
   archive mirror) and invents no consistency findings it cannot judge.

Crash safety
============
A check module that raises is a bug in the validator, not a verdict about the
tree, and it must not cost the operator the other five checks. Every stage
runs inside a guard that turns an unexpected exception into ONE
``runner.check-failed`` error attributed to that check; the run continues with
the next one. ``BaseException`` is not caught — Ctrl-C stays Ctrl-C. The
exception's text is redacted of the tree's own path before it becomes a
finding's detail, because the JSON output is a consumed contract that never
carries a machine path.

The form, and the one tier the modules cannot see
=================================================
The form comes from :func:`lmer_schema.form.detect_form` — presence-only, so a
present-but-BROKEN manifest is still schema form (D20: a typo must never
demote a migrated plexus to the legacy tier). The report carries that form and
resolves every code's severity from it (D14).

One tier decision is invisible to a check module that walks by NAME: a host
bucket's contents are checked at the LEGACY tier's severities even inside a
schema-form tree (SCHEMA.md "Host buckets"), and a bucket never blocks (D9).
The project-list modules carry it themselves — every
:class:`~lmer_schema.checks.walk.ProjectDir` carries the form its CONTENTS
are checked at, ``check_agents`` reads that per project and ``index_links``
derives the same answer from the top-level name — so the runner adds nothing
to their path. ``record`` is the one that cannot: it is deliberately not
project-list-driven (it finds run areas by name, so it works on a bare root)
and it passes no form of its own. It therefore writes through a report VIEW
(:class:`_TierPinnedReport`) that pins any finding under a legacy-form
project dir to the legacy tier, using the ``form`` the walk already
determined. Only a call that leaves the tier to the report is affected, so a
module that decides it itself is never second-guessed.

Scope-outs (§7)
===============
Napkin, knowledge and info CONTENT, the opaque tiers (``_agents/mirrors/``,
``<scope>/<harness>/``, ``_taskdef/``), legacy ``{task_type}/{target}/`` trees
and masterplan bundle internals are out of scope. The runner adds no walk of
its own, so whatever guarantee holds is the composition's: the layout walk
stops at the first project-shaped directory, the agent and index modules are
handed a project list and look only at their own area names inside it, and the
record module looks inside a bundle no further than the one pre-D12 nesting
check D12 allows. The record module does scan directory NAMES at any depth to
find run areas (that is how a bucket's ``_runs/`` is found below its host
segments), so a directory under a scope-out may be LISTED on the way; the
areas it must not enter it prunes by name
(:data:`~lmer_schema.checks.record.OPAQUE_AREAS` and the legacy twins beside
it — which is where ``<project>/_info/`` and legacy ``info/`` are covered),
which is what makes a vendored fork's own ``mirrors/<fork>/runs/`` fixtures
silent rather than an ERROR on a healthy plexus.

The one scope-out no NAME can prune is the legacy ``{task_type}/{target}/``
tree, ``{task_type}`` being an open set; ``record.py`` prunes it by SHAPE
instead (:func:`~lmer_schema.checks.record._is_legacy_task_tree`, issue #379 —
until that landed, a ``runs``-named directory inside one was opened, parsed
and reported, and a tree conformant everywhere else exited 1). Every prune is
``record.py``'s boundary to draw, not the runner's.
``tests/test_lmer_schema_runner.py`` pins every half of this with
booby-trapped files planted where the walk could reach them and a recorder
around the real filesystem calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional, Tuple

from lmer_schema import form as core_form
from lmer_schema.checks import agents as agents_checks
from lmer_schema.checks import index_links as index_checks
from lmer_schema.checks import layout as layout_checks
from lmer_schema.checks import manifest as manifest_checks
from lmer_schema.checks import record as record_checks
from lmer_schema.checks import registry as registry_checks
from lmer_schema.checks.findings import Form, Report, Severity, Tier, register
from lmer_schema.manifest import MANIFEST_NAME, Manifest
from lmer_schema.registry import Registry

__all__ = [
    "CHECK_ORDER",
    "Check",
    "Context",
    "run_checks",
    "validate",
]

#: The plexus root itself — the path a finding about the RUN carries.
_ROOT_PATH = "."

#: What replaces the tree's own path in a redacted exception message.
_REDACTED_ROOT = "<root>"


# ---------------------------------------------------------------------------
# The codes the runner owns
# ---------------------------------------------------------------------------

register(
    "runner.no-such-path",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "the path to validate is not a directory: unusable input (exit 2), "
        "no check runs"
    ),
)
register(
    "runner.check-failed",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "a check module raised an unexpected exception; it is reported as an "
        "error and the remaining checks still run"
    ),
)
register(
    "runner.checks-skipped",
    Severity.INFO,
    tier=Tier.MACHINE,
    summary=(
        "the checks that did not run because the input is unusable — a short "
        "report is never a clean tree"
    ),
)


# ---------------------------------------------------------------------------
# The shared state and the check descriptor
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """What one validation run knows, from the CLI's arguments through to what
    each stage produced for the next.

    ``manifest``/``registry`` are ``None`` both for "there is none" (a legacy
    tree) and for "it did not build"; the stages that consume them treat those
    the same way on purpose — a tree with no usable registry is validated
    exactly like a tree with no registry. ``project_dirs`` is ``None`` until
    the layout walk produces it, and ``None`` is meaningful: the per-project
    modules fall back to their own discovery when the walk never ran (a
    crash), rather than checking nothing at all.
    """

    root: Path
    report: Report
    plexus_repo_url: Optional[str] = None
    now: Optional[datetime] = None
    stale_after: int = record_checks.STALE_AFTER_SECONDS
    manifest: Optional[Manifest] = None
    registry: Optional[Registry] = None
    project_dirs: Optional[Tuple[layout_checks.ProjectDir, ...]] = None

    def tiered(self) -> "_TierPinnedReport":
        """The report view the by-name walkers write through: findings under a
        legacy-form project dir (a host bucket) are pinned to the legacy
        tier's severities (SCHEMA.md "Host buckets")."""
        return _TierPinnedReport(self.report, self.project_dirs or ())


@dataclass(frozen=True)
class Check:
    """One stage of the run: the check ``module`` and the ``run`` adapter that
    knows its entry point's spelling. ``name`` is the module's own short name,
    which is what a ``runner.check-failed`` finding attributes the failure
    to — there is no second spelling to drift."""

    module: ModuleType
    run: Callable[[Context], None]

    @property
    def name(self) -> str:
        return self.module.__name__.rpartition(".")[2]


# ---------------------------------------------------------------------------
# The adapters — the only places that know each module's entry point
# ---------------------------------------------------------------------------


def _check_manifest(context: Context) -> None:
    """``check_manifest(root, report, *, plexus_repo_url=None) -> Manifest``.

    Loads the manifest ONCE for the whole run and marks the report unusable
    itself when it is present but broken (rule 2 above)."""
    context.manifest = manifest_checks.check_manifest(
        context.root, context.report, plexus_repo_url=context.plexus_repo_url
    )


def _check_registry(context: Context) -> None:
    """``check_registry(report, manifest) -> Registry`` — the one entry point
    that takes no root and takes its input positionally.

    ``None`` back means "no usable registry": either the tree is legacy (no
    manifest) or the registry did not build, in which case the error finding
    is already in the report and the run continues registry-free (rule 3)."""
    context.registry = registry_checks.check_registry(
        context.report, context.manifest
    )


def _check_layout(context: Context) -> None:
    """``check_layout(root, report, registry=_LOAD) -> tuple[ProjectDir, ...]``.

    The registry is passed EXPLICITLY — including ``None`` — so the module
    never re-reads the manifest from disk, and the returned project list is
    the one walk of the tree every later stage reuses."""
    context.project_dirs = tuple(
        layout_checks.check_layout(
            context.root, context.report, registry=context.registry
        )
    )


def _check_agents(context: Context) -> None:
    """``check_agents(root, report, *, project_dirs=None) -> None``.

    The report itself, not the tiered view: each ``ProjectDir`` carries the
    form its contents are checked at and ``check_agents`` reads it, so the
    bucket tier arrives with the list (module docstring)."""
    agents_checks.check_agents(
        context.root, context.report, project_dirs=context.project_dirs
    )


def _check_record(context: Context) -> None:
    """``check_record(root, report, *, now=None, stale_after=...) -> None``.

    Walks the record areas by name itself (a bucket's ``_runs/`` sits below
    its host segments, at no fixed depth); *now* and *stale_after* are the
    caller's, so a verdict never depends on the wall clock."""
    record_checks.check_record(
        context.root,
        context.tiered(),
        now=context.now,
        stale_after=context.stale_after,
    )


def _check_index_links(context: Context) -> None:
    """``check_index_links(root, report, *, projects=None) -> None`` — the
    same project list, spelled ``projects=`` here.

    This module decides the bucket tier itself (from the top-level name), so
    the view would leave every one of its findings alone; it gets the report.
    """
    index_checks.check_index_links(
        context.root, context.report, projects=context.project_dirs
    )


#: THE order (module docstring). A check module not listed here never runs —
#: ``tests/test_lmer_schema_runner.py`` compares this list against the
#: package's own modules so that cannot happen quietly.
CHECK_ORDER: Tuple[Check, ...] = (
    Check(manifest_checks, _check_manifest),
    Check(registry_checks, _check_registry),
    Check(layout_checks, _check_layout),
    Check(agents_checks, _check_agents),
    Check(record_checks, _check_record),
    Check(index_checks, _check_index_links),
)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def validate(
    target,
    *,
    plexus_repo_url: Optional[str] = None,
    now: Optional[datetime] = None,
    stale_after: int = record_checks.STALE_AFTER_SECONDS,
    checks: Tuple[Check, ...] = CHECK_ORDER,
) -> Report:
    """Validate the plexus rooted at *target* and return its :class:`Report`.

    *target* IS the plexus root: nothing climbs to a parent. A validator that
    silently validated a DIFFERENT tree than the one it was pointed at would
    be worse than one that says "there is nothing here" — the climbing search
    (:func:`lmer_schema.form.find_plexus_root`) is for tooling that knows only
    where it is standing, not for a command given a path.

    *plexus_repo_url* is the remote the plexus repo itself was cloned from and
    is what the manifest's ``sources:`` same-host guard compares against;
    without it that one comparison is skipped. *now* and *stale_after* are the
    record check's staleness window. *checks* is the pipeline, overridable for
    tests.

    The report carries the form, the findings and the exit code (0 clean, 1
    errors, 2 unusable); this function never raises for a tree and never
    writes anything.
    """
    root = Path(target).expanduser()
    report = Report(core_form.detect_form(root))
    if not root.is_dir():
        report.add(
            "runner.no-such-path",
            _ROOT_PATH,
            "no plexus to validate: the path given is not a directory. "
            "`lmer-schema validate` takes the plexus ROOT — the directory "
            f"holding `{MANIFEST_NAME}` in schema form.",
        )
        report.mark_unusable("no such plexus root: the path given is not a directory")
        return report
    context = Context(
        root=root,
        report=report,
        plexus_repo_url=plexus_repo_url,
        now=now,
        stale_after=stale_after,
    )
    run_checks(context, checks)
    return report


def run_checks(context: Context, checks: Tuple[Check, ...] = CHECK_ORDER) -> Report:
    """Run *checks* in order into *context*, applying the short-circuit and
    crash-safety rules (module docstring). Returns the context's report."""
    report = context.report
    for position, check in enumerate(checks):
        try:
            check.run(context)
        except Exception as exc:  # a validator bug, never a verdict
            _check_failed(report, context.root, check, exc)
        if report.unusable is not None:
            _checks_skipped(report, checks[position + 1:])
            break
    return report


def _check_failed(report: Report, root: Path, check: Check, exc: Exception) -> None:
    report.add(
        "runner.check-failed",
        _ROOT_PATH,
        f"the `{check.name}` check raised and was abandoned; the other checks "
        "still ran, so this report is incomplete rather than wrong. This is a "
        "bug in the validator — please report it.",
        detail=f"{type(exc).__name__}: {_redact(str(exc), root)}",
    )


def _checks_skipped(report: Report, remaining: Tuple[Check, ...]) -> None:
    if not remaining:
        return
    names = ", ".join(check.name for check in remaining)
    report.add(
        "runner.checks-skipped",
        _ROOT_PATH,
        f"{len(remaining)} check(s) did not run: the input is unusable "
        f"({report.unusable}). Nothing below the manifest can be judged "
        "against a registry that could not be read — fix it and run again.",
        detail=f"skipped: {names}",
    )


def _redact(text: str, root: Path) -> str:
    """*text* with the tree's own path replaced by :data:`_REDACTED_ROOT`.

    An exception's message is the one string in a finding the validator did
    not write, and it routinely carries the absolute path the check was
    reading. The JSON output is a consumed contract that never carries a
    machine path, so every ABSOLUTE spelling of the root is redacted, longest
    first — and only those (:func:`_is_machine_path`)."""
    spellings = {str(root), os.path.abspath(root), os.path.realpath(root)}
    for spelling in sorted(spellings, key=len, reverse=True):
        if _is_machine_path(spelling):
            text = text.replace(spelling, _REDACTED_ROOT)
    return text


def _is_machine_path(spelling: str) -> bool:
    """Whether *spelling* of the root may be used as a replacement pattern.

    Redaction is a blind substring replace, so a spelling that is not a
    machine path is pure damage: ``lmer-schema validate .`` is the most
    natural invocation there is, and a bare ``.`` as a pattern is every
    sentence end and every file extension in the message
    (``'bad state.yaml at ./x'`` -> ``'bad state<root>yaml at <root>/x'``);
    ``validate work`` would do the same to the word. Nothing is lost by
    dropping them — the absolute spellings are the ones that carry a machine
    path, they are already in the set, and a relative one names nothing
    outside the caller's own cwd. The filesystem root is excluded on the same
    grounds: ``/`` identifies no machine and matches every separator.
    """
    return os.path.isabs(spelling) and spelling.strip(os.sep) != ""


# ---------------------------------------------------------------------------
# The bucket tier
# ---------------------------------------------------------------------------


class _TierPinnedReport:
    """A :class:`Report` view that pins the severity tier of the paths under a
    legacy-form project dir.

    A host bucket is not part of the schema: its contents are checked at the
    legacy tier's severities in either form (SCHEMA.md "Host buckets"), so a
    bucket never blocks (D9). A module handed the project list carries that
    with the list (each :class:`~lmer_schema.checks.walk.ProjectDir` states
    the form its contents are checked at). ``record`` is not handed one — it
    finds run areas by NAME so that it works on a bare root — and has no
    project to read a form from, so its tier is pinned by PATH here, from the
    same walk's answer: one rule, one place, no second reading of the tree.

    Only a call that leaves the tier to the report (``form=None``) is
    affected: a module that decides the tier itself passes an explicit form
    and is left alone. Every other attribute is the real report's.
    """

    __slots__ = ("_report", "_legacy_prefixes")

    def __init__(self, report: Report, project_dirs) -> None:
        self._report = report
        self._legacy_prefixes = tuple(
            sorted(
                project.rel
                for project in project_dirs
                if getattr(project, "form", None) is Form.LEGACY
            )
        )

    def add(self, code, path, message, *, detail=None, form=None):
        if form is None and self._is_legacy(path):
            form = Form.LEGACY
        return self._report.add(code, path, message, detail=detail, form=form)

    def _is_legacy(self, path) -> bool:
        text = str(path)
        return any(
            text == prefix or text.startswith(f"{prefix}/")
            for prefix in self._legacy_prefixes
        )

    def __getattr__(self, name):
        return getattr(self._report, name)
