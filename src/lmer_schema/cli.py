"""``lmer-schema`` — the validator's command surface (spec §7, D14/D15;
SCHEMA.md "Validator").

This module is the thin outside of ``lmer-schema validate``: it parses argv,
calls :func:`lmer_schema.checks.runner.validate` ONCE and renders the single
:class:`~lmer_schema.checks.findings.Report` it gets back. It owns no check
logic, opens no file of the tree and decides no severity — a rule that is
easy to keep because the report already carries everything the two outputs
need (the form, the ordered findings, the counts, the exit code).

Commands and outputs
====================
``validate <path>`` renders the report, as **text** for a human or as one
**JSON document** for a machine (``--json``); ``--list-checks`` prints the
registered check codes with their severities and tiers — the machine-readable
inventory docs/SCHEMA.md's strictness table is reconciled against, in text or
JSON. ``migrate <path>`` previews an explicit schema-1 to schema-2 cutover,
or applies that reviewed local change with ``--apply``. Its preconditions
and filesystem changes live in :mod:`lmer_schema.migration`; it never commits
or pushes. Legacy layout adoption remains separate.

The exit taxonomy
=================
``0`` clean (warnings and informational findings allowed — **warnings never
block**, D14), ``1`` one or more ERROR findings, ``2`` unusable input. This is
the shape of ``work plan check`` and deliberately NOT the ``0/2/64`` codes of
``lmer_cli.container.sources``. The codes come from the report
(:attr:`Report.exit_code`), never from a count taken here, so the CLI and the
importable core can never disagree about a verdict.

A USAGE error is exit ``2`` as well, and it is the same statement: what the
command was given cannot be validated. It prints to stderr and writes NOTHING
to stdout, so a consumer piping stdout into a JSON parser sees either one
valid document or nothing at all. Stock argparse would exit 2 by calling
``sys.exit`` from inside the parser; :class:`_ArgumentParser` raises instead
so :func:`main` stays a function of argv that returns an int — that is what
makes the whole surface testable without a subprocess.

Where the plexus repo URL comes from
====================================
The manifest's ``sources:`` same-host guard is dormant unless somebody hands
the validator the remote the plexus repo itself was cloned from, and the CLI
is that somebody: ``--plexus-repo-url``. For an arbitrary tree the flag is the
whole answer — ``validate PATH`` takes any root, and a **conformance verdict
must never depend on an ambient variable**, so a work-repo URL that merely
happens to be exported is no evidence about the tree named on the command
line. Judging the same bytes two ways depending on who ran the command would
be a false positive and a false negative in one, and these are ERROR findings
that land in the stored JSON document.

There is exactly one case where the environment IS evidence: when PATH
resolves to ``$LMER_WORK_REPO_PATH``, the tree being validated is this
session's work repo, and ``$LMER_WORK_REPO`` (then the older
``$LMER_REPO_URL``) really is its remote — ``sources doctor``'s precedence,
reused rather than given an LMER_* var of the validator's own. Only then does
the fallback apply. The two paths are compared RESOLVED, so a symlinked,
relative or trailing-slash spelling of one directory is still one repo; an
unset, empty or non-matching ``LMER_WORK_REPO_PATH`` simply means no fallback
— never an error — and the guard stays disarmed unless the flag is given.

The JSON document is a consumed contract
========================================
The hosted connector (lmer-mvp slice 1) stores and re-reads this document, so
its keys are chosen once and pinned by ``tests/test_lmer_schema_cli.py``::

    {"schema": 1, "tool": "lmer-schema",
     "form": "schema", "exit_code": 1, "unusable": null,
     "counts": {"error": 1, "warn": 2, "info": 3},
     "findings": [{"code", "severity", "path", "message", "detail"}, ...]}

Five of those keys ARE :meth:`Report.to_dict`'s output, spliced in as it
stands rather than re-spelled here: the report is the source of that half of
the contract (its own tests pin it), and a CLI that re-assembled it could
drift from the core the connector's other reader uses. The envelope adds
exactly two keys — ``schema``, the document version, so a later shape change
is detectable rather than guessed at (the house convention for every machine
document in this repo), and ``tool``, so a document found on its own says
what wrote it.

What the envelope deliberately does NOT carry is the path that was
validated. Every finding path is repo-relative, the runner redacts the tree's
own path out of even an exception's text, and this document is copied around;
one absolute path in it would leak the machine layout of whoever ran the
validator. The human text output prints the target, because that output goes
to the terminal of the person who typed it.

The text output is an ordered fix-list
======================================
Not a log and not a summary: a numbered list, most severe first (the report's
own order), each entry naming the severity, the stable code and the
repo-relative path, then the message and any detail. Work down it and the
blocking findings are done first. That list is what a LEGACY tree's output is
for — there the validator is a migration aid, not a gate (D14/D16), which the
verdict line says out loud because the exit code (0) says the opposite of
"there is work here".

``--list-checks`` and the codes that cannot fire
================================================
One row per registered code: ``<code> <SEVERITY> <tier> [flags] <summary>``,
in code order, the severity in the second field so the table is greppable and
``awk``-able. A row is only as honest as the code behind it, and one
registered code cannot fire at this lmer's schema support:
``manifest.schema-older`` needs a SUPPORTED schema older than
:data:`~lmer_schema.manifest.CURRENT_SCHEMA`, and at schema 1
``SUPPORTED_SCHEMAS`` is ``(1,)`` — an older ``schema:`` int is unsupported
input (exit 2), never that WARN. Advertising it would promise a lenient
verdict the validator will not give, so the row is flagged
``[unreachable at schema 1]`` (the full reason is the JSON inventory's
``unreachable`` value). The rule is
computed from the schema constants (:func:`unreachable_codes`), not hard-coded
per release, so the flag disappears by itself the day schema 2 ships.

Running it
==========
The console entry ``lmer-schema = lmer_schema.cli:main`` is declared by core
in ``pyproject.toml`` (this subsystem never edits that file), and a console
script only reaches PATH after the editable package is reinstalled — so
``python -m lmer_schema.cli`` is the spelling that always works and the
``__main__`` guard at the bottom is part of the surface, not a nicety.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from lmer_schema.checks.findings import (
    CHECKS,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_UNUSABLE,
    Form,
    Report,
)
from lmer_schema.checks.record import STALE_AFTER_SECONDS
from lmer_schema.checks.runner import validate
from lmer_schema.manifest import CURRENT_SCHEMA, MANIFEST_NAME, SUPPORTED_SCHEMAS

__all__ = [
    "OUTPUT_SCHEMA",
    "PROG",
    "build_parser",
    "inventory_document",
    "json_document",
    "main",
    "render_inventory",
    "render_text",
    "unreachable_codes",
]

#: The console entry's name — the documented spelling of this command, used as
#: argparse's ``prog`` whichever way the module was started.
PROG = "lmer-schema"

#: Version of the JSON documents below. Bumped when their shape changes in a
#: way a reader must notice; every machine document in this repo carries one.
OUTPUT_SCHEMA = 1

#: The work-repo URL variables, in the precedence ``sources doctor``
#: documents — consulted only for the work repo itself (module docstring), so
#: the validator needs no LMER_* var of its own; ``LMER_REPO_URL`` is the
#: older spelling, kept as the fallback.
PLEXUS_URL_ENV = ("LMER_WORK_REPO", "LMER_REPO_URL")

#: Where the work repo is checked out (docs/LMER-CLI.md). This is the value
#: that answers the question the fallback actually needs answered — "is the
#: path being validated the repo those URL variables name?" — so the URLs
#: above are read only when the validated root resolves to it.
PLEXUS_PATH_ENV = "LMER_WORK_REPO_PATH"

#: Seconds in the ``--stale-after`` unit (the record check counts seconds; an
#: operator thinks in days).
_SECONDS_PER_DAY = 86400

#: What a fix-list entry's message and detail are indented by — under the
#: ``NNN. `` number, so the number column reads as the list.
_BODY_INDENT = " " * 5


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    """Raised by :class:`_ArgumentParser` so :func:`main` owns the exit."""


class _ArgumentParser(argparse.ArgumentParser):
    """argparse variant that reports a usage error as a raised exception.

    Stock argparse writes to stderr and calls ``sys.exit`` from inside the
    parser, which would make ``main(argv)`` unusable in-process — the CLI is
    tested by calling it, not by spawning it. Raising lets :func:`main` print
    the message and RETURN :data:`EXIT_UNUSABLE`.
    """

    def error(self, message):  # noqa: D401 - argparse's own contract
        raise _UsageError(f"{self.format_usage().rstrip()}\n{self.prog}: error: {message}")


def build_parser() -> _ArgumentParser:
    """The whole command surface, in one place."""
    parser = _ArgumentParser(
        prog=PROG,
        # Spelled out: the command is optional (--list-checks takes none), and
        # argparse's generated line would show the subcommand as required.
        usage=(
            f"{PROG} validate PATH [--json] [--plexus-repo-url URL] "
            f"[--stale-after DAYS]\n       {PROG} migrate PATH [--apply] [--json]"
            f"\n       {PROG} --list-checks [--json]"
        ),
        description=(
            "Validate a plexus or preview a schema-version migration. "
            "Exit 0 clean (warnings never block), 1 errors, 2 unusable input."
        ),
        epilog=(
            "Validation text is an ordered fix-list; --json prints one document "
            "(schema, tool, form, exit_code, unusable, counts, findings) whose "
            "every path is repo-relative. Run it as `python -m lmer_schema.cli` "
            "when the console script is not on PATH yet."
        ),
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="print the registered check codes with their severities and tiers, "
        "then exit (takes no command)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON document on stdout instead of text",
    )
    # metavar="" so each command is listed once: argparse otherwise prints
    # the choice line AND the choice's own help line under it.
    subparsers = parser.add_subparsers(dest="command", title="commands", metavar="")
    migration_parser = subparsers.add_parser(
        "migrate", help="preview a schema-1 to schema-2 cutover"
    )
    migration_parser.add_argument("path", help="plexus root")
    migration_parser.add_argument("--apply", action="store_true", help="apply a clear preview")
    migration_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    validate_parser = subparsers.add_parser(
        "validate",
        prog=f"{PROG} validate",
        help="validate the plexus rooted at PATH",
        description=(
            "Validate the plexus rooted at PATH. PATH is the root itself — the "
            f"directory holding `{MANIFEST_NAME}` in schema form; nothing climbs "
            "to a parent. A tree with no manifest is legacy form and is checked "
            "at the lenient tier."
        ),
    )
    validate_parser.add_argument("path", help="the plexus root to validate")
    # default=SUPPRESS so `--json` before the command survives the subparser:
    # the subparser writes into the same namespace and a plain default would
    # overwrite the value the top level already resolved.
    validate_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit one JSON document on stdout instead of text",
    )
    validate_parser.add_argument(
        "--plexus-repo-url",
        metavar="URL",
        help="the remote this plexus repo itself was cloned from, for the "
        "manifest's `sources:` same-host guard (default: only when PATH is "
        f"the work repo itself — PATH resolving to ${PLEXUS_PATH_ENV} — "
        f"${PLEXUS_URL_ENV[0]}, then ${PLEXUS_URL_ENV[1]}; for any other "
        "PATH, and without this flag, that one comparison is skipped)",
    )
    validate_parser.add_argument(
        "--stale-after",
        metavar="DAYS",
        type=int,
        default=STALE_AFTER_SECONDS // _SECONDS_PER_DAY,
        help="how long an in-progress session may go untouched before it is "
        "reported as stale, in days (informational; default: %(default)s)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI over *argv* and return its exit code.

    Never raises for a tree and never exits the process: ``--help`` and usage
    errors come back as return values too, so the whole surface is callable
    in-process (``main(["validate", path, "--json"])``).
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return _dispatch(parser, args)
    except _UsageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_UNUSABLE
    except SystemExit as exc:  # argparse printed --help and wanted to exit
        return _exit_code_of(exc)


def _dispatch(parser: _ArgumentParser, args) -> int:
    if args.list_checks:
        if args.command is not None:
            parser.error("--list-checks prints the check inventory and takes no command")
        return _cmd_list_checks(args)
    if args.command is None:
        parser.error("a command is required (try `validate PATH`, or --list-checks)")
    if args.command == "migrate":
        from .manifest import ManifestError
        from .migration import migrate

        try:
            result = migrate(Path(args.path), apply=args.apply)
        except (ManifestError, OSError) as exc:
            print(f"Migration refused: {exc}", file=sys.stderr)
            return EXIT_UNUSABLE
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["diff"] or "Already schema 2; no changes.")
            print("Directory moves: none; historical records remain unchanged.")
            for blocker in result["blockers"]:
                print(f"Blocked: {blocker}")
            if result["applied"]:
                print("Schema 2 applied locally.")
            elif result["diff"] and not result["blockers"]:
                print("Preview only. Re-run with --apply after reviewing and stopping old writers.")
        return EXIT_ERRORS if result["blockers"] else EXIT_CLEAN
    return _cmd_validate(args)


def _exit_code_of(exc: SystemExit) -> int:
    """The int behind a ``SystemExit`` argparse raised: ``--help`` exits 0, a
    message means the invocation was unusable."""
    if exc.code is None:
        return EXIT_CLEAN
    if isinstance(exc.code, int):
        return exc.code
    print(str(exc.code), file=sys.stderr)
    return EXIT_UNUSABLE


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _cmd_validate(args) -> int:
    report = validate(
        args.path,
        plexus_repo_url=_plexus_repo_url(args),
        stale_after=args.stale_after * _SECONDS_PER_DAY,
    )
    if args.json:
        print(json.dumps(json_document(report), indent=2))
    else:
        for line in render_text(report, args.path):
            print(line)
    return report.exit_code


def _plexus_repo_url(args) -> Optional[str]:
    """``--plexus-repo-url``, or the work repo's own URL when — and only when
    — that is what ``args.path`` is (module docstring).

    Supplying this is the CLI's job and nobody else's: without it the manifest
    check's same-host ``sources:`` guard has nothing to compare and stays
    dormant. Dormant is the right answer for a tree the environment says
    nothing about; arming it against a repo that has nothing to do with the
    tree would make the verdict a fact about the machine.
    """
    if args.plexus_repo_url:
        return args.plexus_repo_url
    if not _is_the_work_repo(args.path):
        return None
    for variable in PLEXUS_URL_ENV:
        value = os.environ.get(variable)
        if value:
            return value
    return None


def _is_the_work_repo(target) -> bool:
    """Whether *target* is the checkout ``$LMER_WORK_REPO_PATH`` names.

    Resolved on both sides, so the spellings of one directory (a symlink, a
    trailing slash, a relative path) compare equal. An unset, empty or
    unresolvable value means "not known to be the work repo" — a missing
    variable is not an unusable invocation, it just leaves the fallback with
    nothing to stand on.
    """
    declared = os.environ.get(PLEXUS_PATH_ENV)
    if not declared:
        return False
    try:
        return _resolved(declared) == _resolved(target)
    except OSError:  # an unreadable parent, a symlink loop, a path too long
        return False


def _resolved(value) -> Path:
    """*value* as an absolute path with symlinks and ``..`` taken out. Never
    touches the filesystem's contents and tolerates a path that is not there
    (``strict=False``), because a stale variable is not an error here."""
    return Path(value).expanduser().resolve()


def json_document(report: Report) -> Dict[str, object]:
    """The validate document (module docstring): the report's own document
    under a two-key envelope. Plain types only."""
    return {"schema": OUTPUT_SCHEMA, "tool": PROG, **report.to_dict()}


def render_text(report: Report, target) -> List[str]:
    """The human report: a header, the ordered fix-list, one verdict line."""
    lines = [f"{PROG} validate: {target} — {report.form.value} form", ""]
    if report.unusable is not None:
        lines += [f"unusable input: {report.unusable}", ""]
    findings = report.findings
    if not findings:
        lines.append("no findings — nothing to fix.")
    else:
        lines.append(f"fix list — {len(findings)} finding(s), most severe first:")
        for number, finding in enumerate(findings, 1):
            lines.append(
                f"{number:>3}. {finding.severity.value:<5}  {finding.code}  {finding.path}"
            )
            lines += _continued(finding.message, _BODY_INDENT)
            if finding.detail:
                lines += _continued(finding.detail, _BODY_INDENT, first="detail: ")
    lines += ["", _verdict(report)]
    return lines


def _continued(text: str, indent: str, first: str = "") -> List[str]:
    """*text* as report lines, every line indented under its entry.

    A finding's message or detail can be several lines — a YAML parser's
    complaint about the manifest is the common one — and an unindented
    continuation would run flush against the left margin, where it reads as
    the next item of the fix-list rather than part of this one.
    """
    body = text.splitlines() or [""]
    head = f"{indent}{first}{body[0]}"
    tail = f"{indent}{' ' * len(first)}"
    return [head] + [f"{tail}{line}" for line in body[1:]]


def _verdict(report: Report) -> str:
    counts = report.counts
    tally = (
        f"{counts['error']} error(s), {counts['warn']} warning(s), "
        f"{counts['info']} informational"
    )
    if report.unusable is not None:
        return (
            f"not validated: unusable input — {tally}. Nothing below it could "
            "be judged; fix that and run again."
        )
    if counts["error"]:
        return f"not conformant: {tally}. Fix the errors above, in order."
    return f"conformant: {tally}{_never_blocks(report)}"


def _never_blocks(report: Report) -> str:
    """The tail of a clean verdict — why a list of findings still exits 0."""
    if report.form is Form.LEGACY:
        # D14/D16: a legacy tree is read in place and never has to move, so
        # its list is the migration one — the exit code must not be read as
        # "there is nothing to do here".
        return (
            " — a legacy tree, checked at the lenient tier: the list above is "
            "a migration aid, not a gate."
        )
    return " — warnings never block."


# ---------------------------------------------------------------------------
# --list-checks
# ---------------------------------------------------------------------------


def unreachable_codes() -> Dict[str, str]:
    """Registered codes that cannot fire at this lmer's schema support, and
    why (module docstring).

    Computed from the schema constants rather than listed per release: the day
    ``SUPPORTED_SCHEMAS`` gains an int below ``CURRENT_SCHEMA``, the code
    becomes reachable and drops out of here on its own.
    """
    if any(value < CURRENT_SCHEMA for value in SUPPORTED_SCHEMAS):
        return {}
    known = ", ".join(str(value) for value in SUPPORTED_SCHEMAS)
    return {
        "manifest.schema-older": (
            f"no supported schema is older than {CURRENT_SCHEMA} (supported: "
            f"{known}) — an older `schema:` int is unsupported input, not a warning"
        )
    }


def _cmd_list_checks(args) -> int:
    if args.json:
        print(json.dumps(inventory_document(), indent=2))
    else:
        for line in render_inventory():
            print(line)
    return EXIT_CLEAN


def inventory_document() -> Dict[str, object]:
    """The ``--list-checks`` document: the registry's own rows plus the one
    key the registry cannot know (``unreachable``)."""
    unreachable = unreachable_codes()
    return {
        "schema": OUTPUT_SCHEMA,
        "tool": PROG,
        "checks": [
            {**row, "unreachable": unreachable.get(row["code"])}
            for row in CHECKS.inventory()
        ],
    }


def render_inventory() -> List[str]:
    """One row per code: ``<code> <SEVERITY> <tier> [flags] <summary>``, in
    code order, aligned. The severity is the second field so the table stays
    greppable."""
    entries = list(CHECKS)
    unreachable = unreachable_codes()
    code_width = max((len(entry.code) for entry in entries), default=0)
    tier_width = max((len(entry.tier.value) for entry in entries), default=0)
    lines = []
    for entry in entries:
        flags = []
        if entry.tier_sensitive:
            flags.append(f"[legacy {entry.legacy.value}]")
        if entry.code in unreachable:
            # The row stays one readable line; the full reason is the JSON
            # inventory's `unreachable` value.
            flags.append(f"[unreachable at schema {CURRENT_SCHEMA}]")
        note = f"{' '.join(flags)} " if flags else ""
        lines.append(
            f"{entry.code:<{code_width}}  {entry.severity.value:<5}  "
            f"{entry.tier.value:<{tier_width}}  {note}{entry.summary}"
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
