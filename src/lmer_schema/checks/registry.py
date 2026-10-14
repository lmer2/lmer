"""Registry findings — the §4 corners that are nudges, not refusals.

This module checks the ``projects:`` registry of a manifest that ALREADY
LOADED. Everything it emits is advice about a tree that works; the rules that
make a registry unusable are enforced one layer down and RAISE, because a
manifest that cannot be interpreted is unusable input (validator exit 2, §7),
not a list of findings about a tree.

Who owns which rule
===================
The v1 registry rules of §4 land in three places, and this module is only the
third:

1. :mod:`lmer_schema.manifest` (the loader) owns the §1 short-name charset on
   every ``projects:`` key and the TYPES of ``repo:``/``remotes:``. A
   document that breaks either never parses, so **this module can never see
   a bad short name or a non-string remote** — no code is registered for
   them, deliberately, so ``--list-checks`` never advertises a check that
   cannot fire.
2. :func:`lmer_schema.registry.build_registry` owns the IDENTITY rules (D8):
   ``remotes:`` without ``repo:`` (nothing to clone), the primary repeated
   inside its own ``remotes:``, two values in one entry folding to one key
   (§4: twice within one entry is an error exactly like twice across
   entries), one remote claimed by two entries (resolution must stay
   deterministic), and an embedded credential in any value. All raise
   ``RegistryError``.
3. **This module** owns the soft corners §4 names as nudges: ``remotes: []``
   beside ``repo:`` (pointless, harmless), a non-canonical spelling now that
   ``work adopt`` always writes the canonical form, and a repoless entry
   (D19) with no ``description:`` to say what the tree is for. A repoless
   entry is otherwise never a finding: zero, one and many remotes are the
   same rule at three counts.

Nothing here re-checks layers 1 and 2 — the two guards already pin each other
(``tests/test_lmer_schema_registry.py::TestLayering``) and a third copy would
be the one that drifts. Normalization likewise is never restated: every
spelling question goes through :func:`lmer_schema.urls.normalize`, the thin
adapter over the ONE definition (D21).

The raise, as a finding
=======================
:func:`check_registry` takes the parsed manifest and builds the registry
itself, turning a ``RegistryError`` into ONE ``registry.invalid`` ERROR
finding rather than letting it escape: ``lmer-schema validate`` on a broken
registry should print a fix-list line, not a traceback. The soft nudges are
then SUPPRESSED for that manifest — the registry is one object with one union
of keys, so once an identity rule is broken nothing inside it is trustworthy
and spelling nits printed ahead of the error would be noise. A caller that
wants the exit-2 unusable path instead (the runner may prefer it) calls
``load_registry``/``build_registry`` directly and lets it raise; the taxonomy
is the same either way, since ``RegistryError`` is a ``ManifestError``.

Findings are reported against the manifest itself (``_lmer.yaml``, the
repo-relative path), naming the entry in the message the way the file spells
it — ``projects.<name>.remotes:[1]`` — because that is what the operator
edits. Every value printed goes through ``scrub_credentials`` and the
``RegistryError`` message is stripped of its machine-path prefix, so the JSON
document leaks neither a secret nor a local path.

A legacy tree (no manifest) has no registry: ``check_registry(report, None)``
says nothing. The recognized-legacy INFO is the manifest module's.
"""

from __future__ import annotations

from typing import Iterator, Optional, Tuple

from lmer_cli.container.sources import scrub_credentials
from lmer_schema.checks.findings import Report, Severity, Tier, register
from lmer_schema.manifest import MANIFEST_NAME, Manifest, ManifestError
from lmer_schema.registry import Registry, RegistryError, build_registry
from lmer_schema.urls import normalize

__all__ = ["check_registry"]


register(
    "registry.invalid",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "the `projects:` registry breaks an identity rule (D8/§4) and does "
        "not build — no remote resolves through it"
    ),
)
register(
    "registry.remotes-empty",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="an empty `remotes:` list beside `repo:` (pointless, harmless — §4)",
)
register(
    "registry.non-canonical-remote",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a remote spelled with a scheme, userinfo, a trailing `.git` or a "
        "trailing `/` — the same repo, but not the form tools write"
    ),
)
register(
    "registry.repoless-without-description",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a repoless project (D19) with no `description:` — legal, but nothing "
        "says what the tree is for"
    ),
)


def check_registry(report: Report, manifest: Optional[Manifest]) -> Optional[Registry]:
    """Report on *manifest*'s registry and return the built :class:`Registry`.

    ``None`` for *manifest* is the legacy-form signal (no manifest, no
    registry): nothing is reported. ``None`` comes back when the registry
    does not build, in which case the report carries the
    ``registry.invalid`` ERROR; otherwise the built registry is returned so
    the tree checks reuse it instead of building a second one.
    """
    if manifest is None:
        return None
    try:
        registry = build_registry(manifest)
    except RegistryError as exc:
        report.add(
            "registry.invalid",
            MANIFEST_NAME,
            f"`projects:` is not a usable registry: {_reason(exc)}",
            detail=(
                "no other registry check ran: the union of remotes is what "
                "they compare against."
            ),
        )
        return None
    for name, project in registry.projects.items():
        _check_entry(report, project, manifest.projects[name])
    return registry


def _check_entry(report: Report, project, entry) -> None:
    """The three soft corners of one entry (module docstring, layer 3)."""
    where = f"projects.{project.name}"

    # `remotes: []` WITHOUT `repo:` never reaches here — that is the
    # registry's "nothing to clone" error — so an empty list here always sits
    # beside a primary.
    if entry.get("remotes") == []:
        report.add(
            "registry.remotes-empty",
            MANIFEST_NAME,
            f"`{where}.remotes:` is an empty list and adds nothing: a "
            "single-remote project is one `repo:` line with no list. Drop the key.",
        )

    for label, value in _remote_values(project):
        canonical = normalize(value)
        if value != canonical:
            report.add(
                "registry.non-canonical-remote",
                MANIFEST_NAME,
                f"`{where}.{label}` is spelled {scrub_credentials(value)}, "
                f"which is not the canonical form of the same remote.",
                detail=(
                    f"canonical: {scrub_credentials(canonical)} — tools "
                    "(`work adopt`) write this form; identity is unaffected, "
                    "both spellings resolve to one key."
                ),
            )

    if project.repoless and not (project.description or "").strip():
        report.add(
            "registry.repoless-without-description",
            MANIFEST_NAME,
            f"`{where}` has no `repo:` and no `description:` — a repoless "
            "project is legal (D19), but nothing in the registry says what "
            "this tree is for. Add a one-line `description:`.",
        )


def _remote_values(project) -> Iterator[Tuple[str, str]]:
    """Every remote of *project* as ``(label, value)``, the label spelled the
    way the manifest addresses it (``repo:``, ``remotes:[0]``)."""
    if project.repo is not None:
        yield "repo:", project.repo
    for position, value in enumerate(project.remotes):
        yield f"remotes:[{position}]", value


def _reason(exc: ManifestError) -> str:
    """The registry error's text without its ``<manifest path>: `` prefix.

    The prefix is a MACHINE path, and the JSON output is a consumed contract
    that must never carry one (the finding's own ``path`` field says which
    file this is about). The message itself is already credential-scrubbed by
    the registry.
    """
    return str(exc).removeprefix(f"{exc.path}: ")
