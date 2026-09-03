"""Manifest-envelope checks — the validator's first check module (spec §4/§7).

This module answers one question: is the ``_plexus.yaml`` at the root of this
tree a manifest lmer can use, and what should the operator know about it? It
checks the ENVELOPE — presence, parseability, the ``schema:`` int in both
directions, ``name:``, unknown keys, the ``sources:`` mounts and the
``housekeeping:`` policy — and nothing else. The registry's semantics (the
primary vs additional remotes, duplicate keys, repoless entries, D8/D19) are
``lmer_schema.registry``'s and its own check module's; the on-disk shape is
the tree module's.

Layering — what this module does NOT do
=======================================
``lmer_schema.manifest`` (the loader) owns the §1 short-name charset of a
registry key and the TYPES of ``repo:``/``remotes:``, and ``build_registry``
re-checks them at its API boundary. This module is deliberately NOT a third
copy: it consumes those guarantees. The visible consequence is that a
manifest the loader refuses (a key outside the charset, a ``remotes:`` that
is a bare string) is reported here as an unusable manifest quoting the
loader's own message, rather than as a registry finding — the registry
checks never see a document the loader rejected. Remote-host comparison is
likewise adopted, not restated: the same-host guard uses
``lmer_cli.container.sources``' own trust-rule key (hostname only, port
blind — a second port on the host that already holds the credential is not
another host), the same function ``load_sources`` compares with.

Usable vs unusable — the exit-2 decision
========================================
A present-but-broken manifest is BOTH an ERROR finding and an unusable
report (exit 2, ``Report.mark_unusable``): D20 forbids a broken manifest
demoting a schema-form plexus to legacy, so it can never be swallowed, and
there is no tree-shaped thing left to say anything else about. A ``schema:``
int NEWER than this lmer knows is treated the same way, and that is a
deliberate reading of §4's "the validator reports an error": the finding IS
an ERROR, and the report is additionally unusable because validating a tree
written by a newer schema against schema-1 rules would emit confident
nonsense. The only outcomes that leave the report usable are a clean
manifest, a warned one, and no manifest at all.

Diagnosis reads the raw document
================================
Two things the typed model deliberately drops are exactly what an operator
needs to be told: the unknown top-level keys (kept only as loader warning
TEXT) and the value of a ``schema:`` the loader refused to interpret. So the
checks re-read the manifest as a plain YAML mapping (best effort, never
raising) and diagnose from that. That is a second read of one small file per
run, and it buys findings keyed by code rather than by string-matching the
loader's prose — which would couple every message rewording to this module.

Entry point
===========
``check_manifest(root, report, *, plexus_repo_url=None)`` emits into the
caller's :class:`~lmer_schema.checks.findings.Report` and RETURNS the loaded
:class:`~lmer_schema.manifest.Manifest` (``None`` when there is none or it is
unusable), so the composing runner hands the same parsed model to the sibling
checks instead of loading it again. ``plexus_repo_url`` is the URL the plexus
repo itself was cloned from; it is what the sources same-host guard compares
against, and ``None`` skips that one comparison — exactly as ``load_sources``
skips its trust rule without a work-repo URL, because with no known host
there is nothing to compare and a guess is worse than silence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from lmer_cli.container.sources import (
    SOURCE_ENV_OVERRIDES,
    SOURCES_FILENAME,
    repo_url_host,
    scrub_credentials,
    url_has_embedded_credential,
)
from lmer_schema import manifest as core
from lmer_schema.checks.findings import Severity, Tier, register

__all__ = ["check_manifest", "schema_finding", "ARCHIVE_AFTER_RE"]

#: Where every manifest finding points: the manifest, repo-relative.
_MANIFEST_PATH = core.MANIFEST_NAME

#: The plexus root itself, for the "there is no manifest" finding.
_ROOT_PATH = "."

#: ``housekeeping.<area>.archive_after``: a duration of whole days, or
#: ``null`` for "never auto-archive" (SCHEMA.md housekeeping policy).
ARCHIVE_AFTER_RE = re.compile(r"[0-9]+d")


# ---------------------------------------------------------------------------
# The codes this module owns
# ---------------------------------------------------------------------------

register(
    "manifest.absent",
    Severity.INFO,
    tier=Tier.LEGACY,
    summary=(
        "no `_plexus.yaml` at the plexus root: the tree is legacy form, "
        "recognised and read in place (never an error)"
    ),
)
register(
    "manifest.unparseable",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "a present `_plexus.yaml` that cannot be read or interpreted "
        "(unusable input: a broken manifest never demotes a tree to legacy)"
    ),
)
register(
    "manifest.schema-not-int",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="`schema:` is missing or is not an integer (a YAML bool is not one)",
)
register(
    "manifest.schema-newer",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="`schema:` is newer than this lmer understands; tools refuse to write",
)
register(
    "manifest.schema-older",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="`schema:` is older than the newest this lmer knows; a newer schema is available",
)
register(
    "manifest.schema-unsupported",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="`schema:` is an integer no version of this lmer supports",
)
register(
    "manifest.name-invalid",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="`name:` (the instance name) is missing or is not a non-empty string",
)
register(
    "manifest.unknown-key",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "an unknown top-level or per-entry manifest key, ignored "
        "(schema plasticity: the vocabulary may grow under one `schema:` int)"
    ),
)
register(
    "manifest.sources-reserved",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="a `sources:` name the schema reserves for a later version and does not mount yet",
)
register(
    "manifest.sources-repo-invalid",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="a `sources.<name>` entry with no usable `repo:` remote URL",
)
register(
    "manifest.sources-credential",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="a `sources.<name>.repo` with a credential embedded in the URL",
)
register(
    "manifest.sources-cross-host",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "a `sources.<name>.repo` on a different host than the plexus repo: it "
        "would route the work-repo credential to another host"
    ),
)
register(
    "manifest.sources-legacy-file",
    Severity.WARN,
    legacy=Severity.INFO,
    tier=Tier.LEGACY,
    summary=(
        "a standalone legacy `sources.yaml` (informational in a legacy tree; "
        "the manifest's `sources:` outranks it once one exists)"
    ),
)
register(
    "manifest.housekeeping-policy-invalid",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="a `housekeeping.<area>.archive_after` that is neither a `<N>d` duration nor null",
)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def check_manifest(root, report, *, plexus_repo_url: Optional[str] = None):
    """Check the manifest of the plexus at *root*, emitting into *report*.

    Returns the loaded :class:`~lmer_schema.manifest.Manifest`, or ``None``
    when the tree has no manifest (legacy form) or has one that cannot be
    used (the report is then marked unusable). See the module docstring for
    *plexus_repo_url* and the exit-2 decision.
    """
    root = Path(root)
    path = core.manifest_path(root)
    try:
        manifest = core.load_manifest(root)
    except core.ManifestError as exc:
        _report_broken(report, path, exc)
        return None

    if manifest is None:
        report.add(
            "manifest.absent",
            _ROOT_PATH,
            f"no {core.MANIFEST_NAME}: this is a legacy tree, recognised and "
            "read in place; the validator's findings are the list to fix "
            "before adopting the schema",
        )
        _check_legacy_sources_file(root, report, manifest=None)
        return manifest

    _check_envelope(report, path, manifest)
    _check_sources(report, manifest, plexus_repo_url)
    _check_housekeeping(report, manifest)
    _check_legacy_sources_file(root, report, manifest=manifest)
    return manifest


def schema_finding(
    value, *, supported=None, current=None
) -> Optional[Tuple[str, str]]:
    """The ``(code, message)`` a raw ``schema:`` *value* deserves, or ``None``
    when it is the current int and there is nothing to say.

    Pure and parameterised: *supported* and *current* default to the loader's
    own tuple (read at call time, so a test or a future release changes both
    layers at once), and both directions of §4 finding 9 are expressible
    while v1 supports exactly one int.
    """
    supported = tuple(core.SUPPORTED_SCHEMAS if supported is None else supported)
    current = core.CURRENT_SCHEMA if current is None else current
    known = ", ".join(str(item) for item in supported)
    # Booleans first: `schema: true` is a YAML bool and isinstance(True, int)
    # would otherwise read it as schema 1 (the read_taskdef_schema trap).
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            "manifest.schema-not-int",
            f"`schema:` must be an integer (supported: {known}), got {value!r}",
        )
    if value > current:
        return (
            "manifest.schema-newer",
            f"declares schema {value}, newer than this lmer understands "
            f"(supported: {known}); tools refuse to write to this plexus — "
            "upgrade lmer",
        )
    if value not in supported:
        return (
            "manifest.schema-unsupported",
            f"declares schema {value}, which this lmer does not support "
            f"(supported: {known})",
        )
    if value < current:
        return (
            "manifest.schema-older",
            f"declares schema {value}; schema {current} is available — tools "
            "read and write this tree normally until it is migrated",
        )
    return None


# ---------------------------------------------------------------------------
# Present but broken
# ---------------------------------------------------------------------------


def _report_broken(report, path, exc) -> None:
    """Diagnose a :class:`ManifestError` as precisely as the raw document
    allows, then mark the report unusable (exit 2)."""
    reason = _error_text(exc)
    raw = _raw_document(path)
    diagnosed = False
    if isinstance(raw, dict):
        finding = schema_finding(raw.get("schema"))
        if finding is not None:
            report.add(finding[0], _MANIFEST_PATH, finding[1])
            diagnosed = True
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            report.add(
                "manifest.name-invalid",
                _MANIFEST_PATH,
                "must declare a non-empty string `name:` (the instance name), "
                f"got {name!r}",
            )
            diagnosed = True
    if not diagnosed:
        report.add(
            "manifest.unparseable",
            _MANIFEST_PATH,
            f"a present {core.MANIFEST_NAME} that cannot be used; it is never "
            "silently ignored, so fix it or remove it",
            detail=reason,
        )
    report.mark_unusable(f"{core.MANIFEST_NAME}: {reason}")


def _error_text(exc) -> str:
    """The loader's message without its absolute-path prefix — a finding
    never carries a machine path (the findings model refuses one outright)."""
    text = str(exc)
    prefix = f"{exc.path}: "
    return text[len(prefix):] if text.startswith(prefix) else text


def _raw_document(path) -> Optional[Dict[Any, Any]]:
    """The manifest as a plain mapping, best effort: ``None`` when it cannot
    be read or is not a mapping. Never raises — this runs to explain a
    failure, so it must not become one."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# A manifest that loaded
# ---------------------------------------------------------------------------


def _check_envelope(report, path, manifest) -> None:
    """The version direction and the unknown keys of a loaded manifest. The
    loader has already guaranteed `name:`, the key charset and the
    ``repo:``/``remotes:`` types."""
    finding = schema_finding(manifest.schema)
    if finding is not None:
        report.add(finding[0], _MANIFEST_PATH, finding[1])

    raw = _raw_document(path)
    if isinstance(raw, dict):
        for key in raw:
            if key not in core.TOP_LEVEL_KEYS:
                _unknown_key(report, str(key))
    for name, entry in manifest.projects.items():
        _unknown_entry_keys(report, f"projects.{name}", entry, core.PROJECT_ENTRY_KEYS)


def _unknown_key(report, where) -> None:
    report.add(
        "manifest.unknown-key",
        _MANIFEST_PATH,
        f"unknown manifest key `{where}`, ignored — the schema-owned "
        "vocabulary may grow under one `schema:` int",
        detail=where,
    )


def _unknown_entry_keys(report, prefix, entry, known) -> None:
    for key in entry:
        if key not in known:
            _unknown_key(report, f"{prefix}.{key}")


def _check_sources(report, manifest, plexus_repo_url) -> None:
    """The area mounts: shape, no embedded credential, and the same-host
    trust rule (§4 finding 5). A name this lmer does not mount is warned
    about and otherwise left alone, exactly as ``load_sources`` does — an
    unmounted declaration clones nothing and so carries no credential."""
    plexus_host = _host(plexus_repo_url) if plexus_repo_url else None
    for name, entry in manifest.sources.items():
        where = f"sources.{name}"
        if name in core.RESERVED_SOURCE_NAMES:
            report.add(
                "manifest.sources-reserved",
                _MANIFEST_PATH,
                f"`{where}` is reserved for a later schema and is not mounted "
                "yet; ignored",
                detail=where,
            )
            continue
        if name not in core.SOURCE_NAMES:
            _unknown_key(report, where)
            continue
        # Per NAME: `ref:` is taskdef's alone (SCHEMA.md sources table), and
        # `load_sources` refuses it under any other name at start. The
        # manifest tier is declared-only until its loader lands (issue #370),
        # so an unwarned `sources.napkin.ref` does nothing at all today and
        # refuses to start the day it does.
        _unknown_entry_keys(report, where, entry, core.source_entry_keys(name))

        repo = entry.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            report.add(
                "manifest.sources-repo-invalid",
                _MANIFEST_PATH,
                f"`{where}` must declare a non-empty string `repo:` (the "
                "repository the area is mounted from)",
                detail=f"{where}.repo",
            )
            continue
        repo = repo.strip()
        if url_has_embedded_credential(repo):
            # Never echo the credentialed URL: the finding is printed and
            # serialised as is.
            report.add(
                "manifest.sources-credential",
                _MANIFEST_PATH,
                f"`{where}.repo` embeds a credential; the plexus is shared, so "
                "never commit a token — auth is derived from the work-repo URL "
                "at clone time",
                detail=f"{where}.repo = {scrub_credentials(repo)}",
            )
            continue
        declared_host = _host(repo)
        if not declared_host:
            report.add(
                "manifest.sources-repo-invalid",
                _MANIFEST_PATH,
                f"`{where}.repo` names no host, so it is not a remote lmer can "
                "clone",
                detail=f"{where}.repo = {scrub_credentials(repo)}",
            )
            continue
        if plexus_host is not None and declared_host != plexus_host:
            override = SOURCE_ENV_OVERRIDES.get(name)
            hatch = f"; set {override} for a cross-host source" if override else ""
            report.add(
                "manifest.sources-cross-host",
                _MANIFEST_PATH,
                f"`{where}.repo` is on another host than the plexus repo: a "
                "declared source inherits the plexus credential at clone time, "
                "so this would route it off-host",
                detail=f"{declared_host} is not {plexus_host}{hatch}",
            )


def _host(value) -> str:
    """The trust-rule host of a remote value: the sources loader's own key
    (hostname, port blind), with the registry's canonical spelling handled.

    A manifest value may be written as a full URL (what the sources examples
    show) or in the canonical spelling the registry stores — host and path
    with no scheme, which the URL parser reads as a bare path because there
    is nothing to anchor a host on. §4 defines that spelling as splitting
    deterministically into host and project at the first slash (hostnames
    contain no slash), and §1 makes the dot the mark of a host, which is all
    the fallback uses: no scheme, userinfo or port rules are restated here.
    Empty string when the value names no host — a bare remote NAME is not a
    URL.
    """
    host = repo_url_host(value)
    if host:
        return host
    head = value.split("/", 1)[0] if "/" in value else ""
    return head.lower() if "." in head else ""


def _check_housekeeping(report, manifest) -> None:
    """The TTL policy (§8). Area NAMES are not vetted — SCHEMA.md leaves the
    area set to the housekeeping tool — only the policy's own shape."""
    for area, policy in manifest.housekeeping.items():
        where = f"housekeeping.{area}"
        _unknown_entry_keys(report, where, policy, core.HOUSEKEEPING_POLICY_KEYS)
        if "archive_after" not in policy:
            continue
        value = policy["archive_after"]
        if value is None:
            continue  # explicit "never auto-archive"
        if isinstance(value, str) and not isinstance(value, bool):
            if ARCHIVE_AFTER_RE.fullmatch(value):
                continue
        report.add(
            "manifest.housekeeping-policy-invalid",
            _MANIFEST_PATH,
            f"`{where}.archive_after` must be a `<N>d` duration in whole days "
            "or null (never auto-archive)",
            detail=f"{where}.archive_after = {value!r}",
        )


def _check_legacy_sources_file(root, report, *, manifest) -> None:
    """Precedence sanity (§4): env overrides beat the manifest's ``sources:``,
    which beats a standalone legacy ``sources.yaml``. The standalone file is
    recognised INFO in a legacy tree and WARNed about once a manifest exists,
    because it is then outranked and due to retire."""
    legacy = Path(root) / SOURCES_FILENAME
    if not (legacy.is_symlink() or legacy.exists()):
        return
    if manifest is not None and manifest.sources:
        message = (
            f"a standalone {SOURCES_FILENAME} beside a manifest that declares "
            "`sources:`; the manifest wins (env overrides > manifest sources > "
            f"{SOURCES_FILENAME}) — retire the standalone file"
        )
    else:
        message = (
            f"a standalone legacy {SOURCES_FILENAME}, read in place; fold it "
            "into the manifest's `sources:` when this tree migrates"
        )
    report.add("manifest.sources-legacy-file", SOURCES_FILENAME, message)
