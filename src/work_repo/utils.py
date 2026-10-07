"""Utility functions for work repository.

Also THE work CLI's root resolver, and with it the one place that knows the
work repo's FORM (spec D20, issue #353). :func:`work_repo_layout` answers
"which form is this tree written in, and where is this session's project
dir"; every path helper here is built on it, and sibling modules
(``run_state``, ``specs_index``, ``memory``) route through it rather than
naming ``_plexus.yaml`` or an ``_``-prefixed area themselves. One module
knowing the second set of names is the point: two would drift, and a drifted
pair writes half a session's record at the legacy names and half at the
schema ones — the split tree the validator flags ERROR.
"""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from lmer_schema.manifest import Manifest


class SchemaContextError(ValueError):
    """The work tree declares a schema this writer cannot safely use."""


def load_work_manifest(root: Optional[Path] = None) -> Optional["Manifest"]:
    """Shared version boundary for work-repo writers and metadata readers."""
    root = root if root is not None else work_repo_root()
    path = root / "_plexus.yaml"
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        from lmer_schema.manifest import ManifestError, load_manifest
    except ImportError as exc:
        raise SchemaContextError("schema support is unavailable for this work tree") from exc
    try:
        return load_manifest(root)
    except ManifestError as exc:
        raise SchemaContextError(str(exc)) from exc

# Minimum length for an env var value to be considered a secret worth redacting.
# Very short values (e.g., "yes", "dev", "true") stored in secret-named env vars
# would match common words in normal text, causing false-positive replacements.
# Real API tokens are typically 20+ characters; 8 is a conservative floor.
_MIN_SECRET_LENGTH = 8

# Env var name patterns that indicate sensitive values.
# Mirrors the set used by _redact_env_value in lmer_cli/cli.py; the bare KEY
# subsumes API_KEY (DEPLOY_KEY, SIGNING_KEY, ... all match). Short values are
# still filtered out by _MIN_SECRET_LENGTH, which is what keeps names like
# KEY=yes from turning common words into redactions.
_SECRET_NAME_PATTERNS = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS)", re.IGNORECASE
)

# Secret-named variables whose values are structurally locators rather than
# credentials. Without this exact-name exclusion, the ambient value sweep
# stamps the configured hostname out of every matching URL and emits a false
# leak warning. Known token shapes and URL userinfo remain independently
# redacted from text regardless of the variable that carried them.
_SECRET_VALUE_LOCATOR_NAMES = frozenset({"LMER_GITLAB_TOKEN_HOST"})

# Known token value prefixes — match directly in text regardless of env var name.
# Each pattern captures the full token (prefix + sufficient trailing characters).
# High-entropy tails keep the {20,} floor; the shorter {10,} families (Slack,
# GitLab runner/deploy tokens) are distinctive enough on their prefix alone.
_SECRET_VALUE_PREFIX_RE = re.compile(
    r"("
    r"glpat-[A-Za-z0-9_\-.]{20,}"  # GitLab personal access token
    r"|gl(?:rt|dt)-[A-Za-z0-9_\-.]{10,}"  # GitLab runner / deploy token
    r"|sk-[A-Za-z0-9_\-.]{20,}"  # LLM API key
    r"|github_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained PAT
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub classic PAT / OAuth / app tokens
    r"|xox[bp]-[A-Za-z0-9-]{10,}"  # Slack bot / user token
    r"|xapp-[A-Za-z0-9-]{10,}"  # Slack app-level token
    r")"
)

# NOTHING in either pattern may depend on what PRECEDES a prefix. Where a token
# can start is not enumerable: it follows an ANSI colour code (whose final `m`
# is a word character), a JSON-escaped newline (a literal `n`), a hyphen or an
# underscore in a slug, a letter, a digit, a percent-escape. A left boundary
# was tried to stop `lmer-task-worker-…` being recorded as
# `lmer-ta***REDACTED***`, and it let real tokens through every one of those,
# here and at every `redact_secrets` call site. A miss publishes a credential
# to a shared repository; an over-redaction costs a readable hostname. False
# positives may be reduced only by constraining what FOLLOWS a prefix (body
# length and alphabet per provider), and only with proof that no real token is
# excluded.
#
# Token SHAPES, for data that arrives from outside this process (the session
# metadata envelope: platform `session_metadata`, a hand-set env value). The
# prefix list above is tuned for prose — logs and reports — where a {20,} tail
# keeps a sentence mentioning "glpat-" readable. Metadata is a record in a
# shared repository, and a token there need not be one this process holds, so
# the floor is the shape itself: a known prefix with any plausible tail, AWS
# key ids, and PEM blocks holding a private key (through their END line when present).
_TOKEN_SHAPE_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|.*)"  # PEM private key
    r"|glpat-[A-Za-z0-9_\-.]{8,}"  # GitLab personal access token
    r"|gl(?:rt|dt)-[A-Za-z0-9_\-.]{8,}"  # GitLab runner / deploy token
    r"|github_pat_[A-Za-z0-9_]{8,}"  # GitHub fine-grained PAT
    r"|gh[pousr]_[A-Za-z0-9]{8,}"  # GitHub classic PAT / OAuth / app tokens
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"  # AWS access key id: exactly 20, judged by what follows
    r"|xox[abprs]-[A-Za-z0-9-]{8,}"  # Slack app / bot / user tokens
    r"|xapp-[A-Za-z0-9-]{8,}"  # Slack app-level token
    r"|sk-[A-Za-z0-9_\-.]{20,}",  # LLM API key
    re.DOTALL,
)

# Backstop for credentials embedded in URLs (https://oauth2:<token>@host/path).
# Replaces the whole userinfo segment regardless of token shape. The userinfo
# cannot contain "/", whitespace or "@", so plain URLs and bare email addresses
# (no "://" before them) are left alone.
_URL_USERINFO_RE = re.compile(r"(://)[^/\s@]+(@)")

_REDACTED = "***REDACTED***"


def _collect_secret_values() -> list[str]:
    """
    Collect sensitive values from environment variables.

    Scans all env vars whose names match common secret patterns
    (TOKEN, KEY, SECRET, PASSWORD, CREDENTIALS), excluding known locator
    variables, and returns their values sorted longest-first so longer tokens
    are replaced before any shorter substrings.

    Returns:
        List of secret values (longest first)
    """
    secrets = set()
    for name, value in os.environ.items():
        if (
            name.upper() not in _SECRET_VALUE_LOCATOR_NAMES
            and _SECRET_NAME_PATTERNS.search(name)
            and len(value) >= _MIN_SECRET_LENGTH
        ):
            secrets.add(value)
    # Sort longest-first to avoid partial replacements
    return sorted(secrets, key=len, reverse=True)


def redact_secrets(text: str, secret_values: Optional[list[str]] = None) -> str:
    """
    Replace any secret values found in text with a redaction marker.

    Scans the text for values of environment variables whose names match
    sensitive patterns (TOKEN, KEY, SECRET, PASSWORD, CREDENTIALS), for
    values carrying a known token prefix, and for credentials embedded in
    URLs. Any matches are replaced with ***REDACTED*** and a warning is
    printed to stderr.

    Args:
        text: The text to redact
        secret_values: Pre-collected secret values (if None, collected from env)

    Returns:
        Text with secrets replaced by ***REDACTED***
    """
    if not text:
        return text

    if secret_values is None:
        secret_values = _collect_secret_values()

    redacted = text
    found = False
    for secret in secret_values:
        if secret in redacted:
            redacted = redacted.replace(secret, _REDACTED)
            found = True

    # Also redact tokens matching known value prefixes (e.g. glpat-, ghp-, xoxb-)
    if _SECRET_VALUE_PREFIX_RE.search(redacted):
        redacted = _SECRET_VALUE_PREFIX_RE.sub(_REDACTED, redacted)
        found = True

    # Backstop: strip userinfo from URLs, whatever the credential looks like
    if _URL_USERINFO_RE.search(redacted):
        redacted = _URL_USERINFO_RE.sub(rf"\g<1>{_REDACTED}\g<2>", redacted)
        found = True

    if found:
        print(
            "⚠️  Warning: secret value(s) detected and redacted from output",
            file=sys.stderr,
        )

    return redacted


def redact_token_shapes(text: str) -> str:
    """Replace token-shaped substrings, whether or not this process holds them.

    The complement of :func:`redact_secrets` for data supplied from outside:
    that function knows the values in this process's environment; this one
    knows what a credential looks like (:data:`_TOKEN_SHAPE_RE`).
    """
    return _TOKEN_SHAPE_RE.sub(_REDACTED, text) if text else text


def is_secret_env_name(name: str) -> bool:
    """True if an env var name matches the sensitive-name pattern.

    The shared name rule (TOKEN/KEY/SECRET/PASSWORD/CREDENTIALS) behind the
    redaction sinks and the prompt/taskdef render-context filters, exposed so
    callers outside this module never grow their own copy of the regex.
    """
    return bool(_SECRET_NAME_PATTERNS.search(name))


def strip_url_credentials(url):
    """Strip any embedded ``user:password@`` credentials from a URL.

    Unlike :func:`redact_secrets`, which stamps a redaction marker into free
    text, this keeps the URL usable: scheme/host/port/path survive and only
    the userinfo is removed (``https://oauth2:tok@host/p`` →
    ``https://host/p``). Values that are not credentialed URLs pass through
    unchanged — including scp-style SSH remotes (``git@host:path``), whose
    userinfo is protocol plumbing, not a credential.
    """
    if not url or "://" not in url or "@" not in url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return url
    except Exception:
        # Fail closed: never return a value that may still carry the
        # credential when parsing fails (e.g. an out-of-range port makes
        # `parsed.port` raise). Strip the userinfo with a regex that cannot
        # raise instead.
        return re.sub(r"(://)[^/]*@", r"\1", url)


def sanitize_task_target(task_target: str) -> str:
    """
    Sanitize task target for use in filesystem paths.

    Handles various formats:
    - URLs: Extract meaningful identifier (MR/PR number, branch name, etc.)
    - Branch names: Use as-is with minimal sanitization
    - Commit SHAs: Use as-is

    Args:
        task_target: Task target string (URL, branch name, commit SHA, etc.)

    Returns:
        Sanitized string safe for filesystem use
    """
    if not task_target:
        return "default"

    # If it's a URL, try to extract meaningful parts
    if task_target.startswith("http://") or task_target.startswith("https://"):
        # GitLab MR format: .../-/merge_requests/123
        if "/-/merge_requests/" in task_target.lower():
            parts = task_target.split("/-/merge_requests/")
            if len(parts) > 1:
                mr_id = parts[-1].split("/")[0].split("?")[0]
                return f"mr-{mr_id}"
        # GitLab issue format: .../-/issues/456
        if "/-/issues/" in task_target.lower():
            parts = task_target.split("/-/issues/")
            if len(parts) > 1:
                issue_id = parts[-1].split("/")[0].split("?")[0]
                return f"issue-{issue_id}"
        # GitLab work item format (newer GitLab UI): .../-/work_items/70
        # Work items are issues; normalize to the same issue-{id} form so a
        # work_items URL and its equivalent /-/issues/ URL share a target.
        if "/-/work_items/" in task_target.lower():
            parts = task_target.split("/-/work_items/")
            if len(parts) > 1:
                issue_id = parts[-1].split("/")[0].split("?")[0]
                return f"issue-{issue_id}"
        # GitHub PR format: .../pull/123
        if "/pull/" in task_target.lower():
            parts = task_target.split("/pull/")
            if len(parts) > 1:
                pr_id = parts[-1].split("/")[0].split("?")[0]
                return f"pr-{pr_id}"
        # GitHub issue format: .../issues/456
        if "/issues/" in task_target.lower() and "/pull/" not in task_target.lower():
            parts = task_target.split("/issues/")
            if len(parts) > 1:
                issue_id = parts[-1].split("/")[0].split("?")[0]
                return f"issue-{issue_id}"
        # Fallback: use the last meaningful segment of the URL
        path_parts = [p for p in task_target.split("/") if p and p not in ("http:", "https:", "")]
        if path_parts:
            # Take last part, remove query params and fragments
            last_part = path_parts[-1].split("?")[0].split("#")[0]
            return last_part.replace(":", "-")

    # For branch names, commit SHAs, etc., sanitize but preserve structure
    # Replace problematic characters but keep alphanumeric, dashes, underscores, dots
    sanitized = "".join(c if c.isalnum() or c in "-_." else "-" for c in task_target)
    # Collapse multiple dashes
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    # Remove leading/trailing dashes
    sanitized = sanitized.strip("-")

    return sanitized if sanitized else "default"


# ---------------------------------------------------------------------------
# Form detection and project-dir resolution (spec D20 — dual read)
# ---------------------------------------------------------------------------

#: The two forms, spelled as the core's ``lmer_schema.form.Form`` values.
#: Plain strings, not that enum, so a build without the core (see
#: :func:`work_repo_layout`) can still be routed on.
SCHEMA_FORM = "schema"
LEGACY_FORM = "legacy"

#: The manifest's name, spelled here and nowhere else in the work CLI. The
#: core owns it too (``lmer_schema.manifest.MANIFEST_NAME``) and every read of
#: the file goes through the core; this copy answers the ONE question that has
#: to be answerable WITHOUT the core — is anything schema-shaped present? —
#: which is what separates "no core installed" from "the core is here and
#: broken" (see :func:`_core_missing`).
MANIFEST_NAME = "_plexus.yaml"

#: What a layout's ``problem`` is about. ``MANIFEST_PROBLEM``: the document
#: itself is unusable (unparseable, unreadable, a schema int this build does
#: not know) or unreadable by this build at all — nothing resolves.
#: ``REGISTRY_PROBLEM``: the document loaded and its ``projects:`` block
#: breaks a rule — only the identity layer is unusable, so the base still
#: resolves to the host bucket.
MANIFEST_PROBLEM = "manifest"
REGISTRY_PROBLEM = "registry"

#: Problems already announced on stderr, so a resolver called a dozen times
#: in one command says it once (the announce-once convention).
_ANNOUNCED_MANIFEST_PROBLEMS: set = set()


@dataclass(frozen=True)
class WorkRepoLayout:
    """Where this session writes, and which names it writes under.

    ``form`` is :data:`SCHEMA_FORM` or :data:`LEGACY_FORM`; ``root`` the work
    repo (``LMER_WORK_REPO_PATH``, default ``/work``); ``base`` this session's
    project dir, or ``None`` when there is nothing to resolve (no run context)
    or nothing safe to resolve (an unusable manifest, ``problem``).

    ``project`` is the registry short name, populated ONLY on a registry hit —
    an unregistered repo resolves to its ``{host}/{project}`` host bucket with
    ``project=None``, because names are never derived (D9). ``manifest`` says
    where the manifest is (schema form only), never what it says.

    ``problem_kind`` says which layer the ``problem`` is about
    (:data:`MANIFEST_PROBLEM` or :data:`REGISTRY_PROBLEM`), so a caller can
    tell "this tree cannot be read" from "this tree's identity layer cannot
    be read" without re-deriving it from ``base``. ``None`` whenever
    ``problem`` is.
    """

    form: str
    root: Path
    base: Optional[Path]
    project: Optional[str]
    manifest: Optional[Path]
    problem: Optional[str]
    problem_kind: Optional[str] = None

    @property
    def is_schema(self) -> bool:
        """Whether the tree is written in the schema form."""
        return self.form == SCHEMA_FORM


def work_repo_root() -> Path:
    """The work repo root: ``LMER_WORK_REPO_PATH``, ``/work`` when unset."""
    return Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))


def work_repo_layout() -> WorkRepoLayout:
    """Resolve the work repo's form and this session's project dir (D20).

    THE accessor: every path helper here goes through it and so should every
    sibling module, which is what keeps the second set of names in one place.

    **Legacy form** (no ``_plexus.yaml`` at the root) resolves exactly as it
    always has — ``{root}/{LMER_REPO_HOST}/{LMER_REPO_PROJECT}``, ``None``
    when either is unset. **Schema form** looks the launch's repo URL
    (``LMER_REPO_URL``, else ``https://{host}/{project}``) up across every
    registry entry's ``repo:`` and ``remotes:`` (D8) and resolves to
    ``{root}/<short-name>/``; with no match the work goes to the
    ``{host}/{project}`` host bucket and stays there, visibly unnamed, until
    a human names it (D9). A dotless host has no bucket (§1), so it falls
    back to the same ``{host}/{project}`` path lmer already writes rather
    than resolving nowhere and losing the work.

    Three failure modes, deliberately different (D20). The line through all
    of them: a PRESENT manifest is never read as a legacy tree, because that
    fallback would write this session's record into ``{host}/{project}/runs/``
    inside a schema-form tree — a silent demotion, and a split tree the
    validator flags ERROR.

    * The **core API is not importable and no manifest is present** — an lmer
      built without ``lmer_schema`` at all, pointed at an unmigrated tree.
      Nothing schema-shaped is there, so the tree reads as legacy, silently
      and without raising: hook-facing commands that must always exit 0 sit
      on this resolver, and D20 cuts over lmer VERSIONS, not repos. Presence
      is the whole question and it needs no core — one ``stat`` (see
      :func:`_core_missing`), which is what keeps "there is no core" apart
      from "the core is here and broken" (a partial install, a broken
      transitive import, a defect in this build). An ``ImportError`` with a
      manifest present is the case below, not this one.
    * The **manifest cannot be read** — unparseable, not a mapping, a schema
      int this build does not support, an unreadable file, or a core this
      build cannot import at all. This NEVER degrades to legacy. It resolves
      no base and reports ``problem`` (which names the manifest) with
      ``problem_kind=`` :data:`MANIFEST_PROBLEM`. Mutating verbs refuse;
      read-only and hook verbs report it and carry on, still exiting 0.
    * The **manifest reads but its registry breaks a rule** — a bad entry,
      one remote claimed twice, ``remotes:`` with no ``repo:``. The document
      parsed, so only the IDENTITY layer is unusable, exactly as the
      validator's runner reads the same input (its rule 3): the base resolves
      to the ``{host}/{project}`` host bucket in SCHEMA form and ``problem``
      is reported with ``problem_kind=`` :data:`REGISTRY_PROBLEM`. The bucket
      is D9's documented holding place for work whose name nobody has
      settled, so this is a fallback WITHIN the form, not a demotion out of
      it, and D20 holds. Failing closed here instead would refuse a base to
      every session against the work repo — including sessions whose repo is
      unregistered and would have resolved to that same bucket without
      consulting the registry at all — over one hand-edited entry. D9's rule
      is that work is never refused and never lost: a session that resolves
      nothing records NOTHING, while a session that records into the bucket
      while its entry exists leaves a split tree the validator flags ERROR —
      visible, recoverable, and the better failure of the two.

    Nothing is cached. The launch context is mutated at runtime (a masterplan
    sub-launch sets ``LMER_REPO_HOST``/``LMER_REPO_PROJECT`` for its own
    resolution) and adoption writes the manifest while a container is up, so
    a per-process cache would answer with the tree as it used to be. The cost
    is one ``stat`` per call in a legacy tree and one small YAML parse in a
    schema one.
    """
    root = work_repo_root()
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    legacy_base = (
        root / repo_host / repo_project if repo_host and repo_project else None
    )

    schema = _schema_layout(root, repo_host, repo_project, legacy_base)
    if schema is not None:
        return schema
    return WorkRepoLayout(
        form=LEGACY_FORM,
        root=root,
        base=legacy_base,
        project=None,
        manifest=None,
        problem=None,
    )


def work_repo_form() -> str:
    """The form of the work repo — :data:`SCHEMA_FORM` or :data:`LEGACY_FORM`."""
    return work_repo_layout().form


def work_repo_manifest_problem() -> Optional[str]:
    """Why the work repo's ``_plexus.yaml`` cannot be used, or ``None``.

    The reason surfaced for callers that report rather than resolve (a
    mutating verb refusing to write, a hook printing one line and exiting 0).
    ``None`` when the tree is legacy or its manifest is fully usable. A
    problem does not always mean nothing resolved — a registry-rule problem
    still resolves the host bucket; see ``problem_kind`` on
    :class:`WorkRepoLayout` for the two cases.
    """
    return work_repo_layout().problem


def work_repo_project_rels(root: Path) -> list[str]:
    """Repo-relative dirs of every project of the plexus at *root*.

    Registered projects first, in registry order, then the host buckets —
    exactly what the core's walk yields for the tree's own registry — and
    ``[]`` on a legacy tree (no manifest), where there is no project set to
    speak of. This is the work CLI's one spelling of "which directories are
    projects" (the publication choke point indexes exactly these), kept here
    because naming the core's walk IS re-deriving the form (issue #353 D20).

    A present manifest the core cannot use raises what the core raises
    (``ManifestError``, ``RegistryError``), and a build without the core
    raises ``ImportError``: the caller decides its own fallback, never this
    function, which would otherwise have to guess at a project set.
    """
    from lmer_schema.checks.walk import project_dirs
    from lmer_schema.registry import load_registry

    registry = load_registry(root)
    if registry is None:
        return []
    return [project.rel for project in project_dirs(root, registry=registry)]


def _schema_layout(root, repo_host, repo_project, legacy_base):
    """The schema-form layout, or ``None`` when this is not that case.

    ``None`` means "read the tree at its legacy names": a root with no
    manifest, with or without the core API. A PRESENT manifest is never this
    case — it returns a layout in schema form however badly it reads (see
    :func:`work_repo_layout`).
    """
    try:
        # Submodule imports, inside the call: the core is imported only when
        # a work-repo path is actually resolved, and a build that ships
        # without it (or with it half-installed) degrades instead of dying on
        # a module-level import in a hook path.
        from lmer_schema.form import Form, detect_form
        from lmer_schema.manifest import ManifestError, load_manifest, manifest_path
        from lmer_schema.registry import (
            RegistryError,
            load_registry,
            resolve_project_dir,
        )
    except ImportError as exc:
        return _core_missing(root, exc)

    if detect_form(root) is not Form.SCHEMA:
        return None

    manifest = manifest_path(root)
    registry, problem = None, None
    try:
        manifest_schema = load_manifest(root).schema
        registry = load_registry(root)
    except RegistryError as exc:
        # The identity layer only: the document parsed, so the tree, its form
        # and every non-registry surface still say something true (the same
        # reading the validator's runner gives this input). Resolution
        # continues registry-free, which is bucket-only — the path this
        # session would have taken anyway if its repo is unregistered.
        problem = _announce(
            str(exc),
            "Resolving to the {host}/{project} host bucket instead: the "
            "manifest parsed, so only its registry is unusable (spec D9/D20) "
            "— work is parked, never refused. Fix the entry, then move it.",
        )
    except ManifestError as exc:
        return _unusable(root, manifest, str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        # Whatever went wrong reading a PRESENT manifest, the one thing that
        # must not happen is falling back to the legacy names (D20).
        return _unusable(root, manifest, f"{manifest}: unusable manifest: {exc}")

    url = os.environ.get("LMER_REPO_URL")
    if not url and repo_host and repo_project:
        url = f"https://{repo_host}/{repo_project}"

    base, project = legacy_base, None
    if url:
        try:
            resolution = resolve_project_dir(
                root, url, registry=registry, schema=manifest_schema
            )
        except RegistryError as exc:
            return _unusable(root, manifest, str(exc))
        if resolution.path is not None:
            base, project = resolution.path, resolution.project
        elif resolution.bucket_path is not None:
            # A miss with no bucket dir yet: the first launch for an unnamed
            # repo creates it. Never a derived short name (D9).
            base = resolution.bucket_path
    return WorkRepoLayout(
        form=SCHEMA_FORM,
        root=root,
        base=base,
        project=project,
        manifest=manifest,
        problem=problem,
        problem_kind=REGISTRY_PROBLEM if problem else None,
    )


def _core_missing(root, exc) -> Optional[WorkRepoLayout]:
    """What an ``ImportError`` of the core API means for THIS tree.

    Entirely a question of what is on disk, and the only question this module
    answers without the core. ``ImportError`` cannot tell "this lmer ships no
    ``lmer_schema``" from "the core is here and something under it is broken"
    — a partial install, a broken transitive import, a defect in this build —
    and the degrade-to-legacy clause was written for the first only, whose
    premise is that nothing schema-shaped is present.

    So: no manifest, degrade (``None``, an old checkout against an unmigrated
    tree). A manifest present, and this build cannot read it: that is a
    problem, not a legacy tree (D20). Presence is tested exactly as the core
    tests it — ``is_symlink()`` first, because a dangling symlink is someone
    having put a manifest there even though ``exists()`` follows it and says
    no.
    """
    manifest = Path(root) / MANIFEST_NAME
    if not (manifest.is_symlink() or manifest.exists()):
        return None
    return _unusable(
        root,
        manifest,
        f"{manifest}: present, but the lmer-schema core API is not importable "
        f"in this build ({exc})",
    )


def _unusable(root, manifest, problem) -> WorkRepoLayout:
    """The fail-closed layout for a manifest this build cannot read: schema
    form, no base, and the reason announced once so nothing is silent."""
    return WorkRepoLayout(
        form=SCHEMA_FORM,
        root=root,
        base=None,
        project=None,
        manifest=manifest,
        problem=_announce(
            problem,
            "Refusing to resolve a work-repo path: a present _plexus.yaml is "
            "never read as a legacy tree (spec D20). Fix or remove the manifest.",
        ),
        problem_kind=MANIFEST_PROBLEM,
    )


def _announce(problem: str, remedy: str) -> str:
    """Print *problem* and its *remedy* to stderr once per process, and
    return the problem so the caller can record it on the layout.

    Once, because this resolver is called a dozen times in one command and
    the operator needs the line, not the chorus (the announce-once
    convention). Never raised and never on stdout: hook-facing verbs sit on
    this resolver and must still exit 0 with their own output intact.
    """
    if problem not in _ANNOUNCED_MANIFEST_PROBLEMS:
        _ANNOUNCED_MANIFEST_PROBLEMS.add(problem)
        print(f"❌ {problem}\n   {remedy}", file=sys.stderr)
    return problem


def _work_repo_base() -> Optional[Path]:
    """
    Return this session's project dir inside the work repo.

    ``{LMER_WORK_REPO_PATH}/{host}/{project}`` in a legacy tree — the
    env-var trio every work-repo path has always been built from — and the
    registry-resolved (or host-bucket) project dir in a schema-form one; see
    :func:`work_repo_layout`, which this delegates to.

    Returns ``None`` if the host or project is unset, since no meaningful
    path can be assembled without them, and also when a present manifest is
    unusable (fail-closed, D20). Callers keep ownership of their own
    "missing env var" error UX.

    Returns:
        The base ``Path``, or ``None`` if no base can be resolved.
    """
    return work_repo_layout().base


def project_area(legacy_name: str, schema_name: str) -> Optional[Path]:
    """
    Return the project-scope area dir named *legacy_name* in a legacy tree
    and *schema_name* in a schema-form one.

    The routing primitive the area helpers below are built from, and the one
    sibling modules use for the areas they own (``runs``/``_runs``,
    ``specs``/``_specs``) so the legacy→schema mapping stays in one module.
    *schema_name* may be a multi-segment name (``_agents/memory``). Returns
    ``None`` whenever no base resolves.
    """
    layout = work_repo_layout()
    if layout.base is None:
        return None
    return layout.base / (schema_name if layout.is_schema else legacy_name)


def project_info_dir() -> Optional[Path]:
    """
    Return the project-wide info directory: ``{project}/info`` in a legacy
    tree, ``{project}/_info`` in a schema-form one.

    Returns ``None`` when no base resolves (see :func:`_work_repo_base`).
    """
    return project_area("info", "_info")


def project_memory_dir() -> Optional[Path]:
    """
    Return the project-scope memory directory: ``{project}/memory`` in a
    legacy tree, ``{project}/_agents/memory`` in a schema-form one.

    Persisted agent memory shared across all task types and targets for the
    project. In schema form this scope holds `project`-type memory only; the
    other types live at :func:`instance_memory_dir` (D17). Returns ``None``
    when no base resolves.
    """
    return project_area("memory", "_agents/memory")


def instance_memory_dir() -> Optional[Path]:
    """
    Return the instance-scope memory directory ``{work repo}/_agents/memory``,
    or ``None`` in a legacy tree.

    The second memory scope (D17): `user`/`feedback`/`reference` memories are
    true of the operator, not of one project, so in schema form they live once
    at the plexus root. A legacy tree has no such scope — every type lands in
    :func:`project_memory_dir` — and ``None`` says so rather than naming a
    directory that form does not have. Needs no run context: it hangs off the
    root, so it resolves even when host/project are unset. Still ``None`` when
    the manifest itself cannot be read (fail-closed, D20) — but a broken
    REGISTRY does not reach it: this scope is at the root and asks the
    identity layer nothing.
    """
    layout = work_repo_layout()
    if not layout.is_schema or layout.problem_kind == MANIFEST_PROBLEM:
        return None
    return layout.root / "_agents" / "memory"


def task_info_dir() -> Optional[Path]:
    """
    Return ``{LMER_WORK_REPO_PATH}/{host}/{project}/{task}/info``.

    This is the task-specific info directory. ``LMER_TASK`` defaults to
    ``"default"`` when unset. Returns ``None`` if ``LMER_REPO_HOST`` or
    ``LMER_REPO_PROJECT`` is unset.
    """
    base = _work_repo_base()
    if base is None:
        return None
    task_type = os.environ.get("LMER_TASK", "default")
    return base / task_type / "info"


def task_target_dir() -> Optional[Path]:
    """
    Return ``{LMER_WORK_REPO_PATH}/{host}/{project}/{task}/{task_target}``.

    The task target directory holds the log file and report files for the
    current run. ``LMER_TASK`` and ``LMER_TASK_TARGET`` default to
    ``"default"`` when unset; the task target is passed through
    :func:`sanitize_task_target` to match the on-disk directory naming.
    Returns ``None`` if ``LMER_REPO_HOST`` or ``LMER_REPO_PROJECT`` is unset.
    """
    base = _work_repo_base()
    if base is None:
        return None
    task_type = os.environ.get("LMER_TASK", "default")
    task_target = os.environ.get("LMER_TASK_TARGET", "default")
    safe_task_target = sanitize_task_target(task_target) if task_target else "default"
    return base / task_type / safe_task_target


def no_run_context_error() -> str:
    """The mutating verbs' refusal line, naming the cause it actually has.

    A verb refuses when nothing resolves a work-repo path, and until the work
    repo had a second form there was one way for that to happen: the launch
    context (``LMER_REPO_HOST``/``LMER_REPO_PROJECT``) was not set. A tree
    whose manifest cannot be read is a second way (issue #353 D20) — the
    resolver fails closed rather than writing this session's record at the
    legacy names inside a schema-form tree — and reporting it as unset env
    vars sends the fix at the environment instead of at the manifest.

    Lives here rather than in ``cli`` because ``git_ops`` needs it too and
    ``cli`` imports ``git_ops`` — this module is the one both already
    import, and it owns ``work_repo_layout``.

    Read on ``problem_kind``, never on ``problem`` alone, because the two
    problems mean opposite things to a verb. A MANIFEST problem resolves no
    base, so it IS the reason there is no run context. A REGISTRY problem
    resolves one anyway — the ``{host}/{project}`` host bucket, D9's holding
    place — so a verb that refuses on it would take the whole work repo down
    over one hand-edited entry, and a verb that merely REPORTS it here would
    blame a broken registry for env vars nobody set (the two coincide: an
    unregistered launch with no host/project resolves nothing either).
    """
    return f"❌ No run context: {no_run_context_reason()}"


def no_run_context_reason() -> str:
    """The cause alone, for a caller that frames it itself (the read-only
    verbs' note, the `--json` `detail` field)."""
    layout = work_repo_layout()
    if layout.problem and layout.problem_kind == MANIFEST_PROBLEM:
        return layout.problem
    return "LMER_REPO_HOST and LMER_REPO_PROJECT must be set"
