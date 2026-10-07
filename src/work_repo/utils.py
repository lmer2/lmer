"""Utility functions for work repository.

Also THE work CLI's root resolver, and with it the one place that knows the
work repo's FORM (spec D20, issue #353). :func:`work_repo_layout` answers
"which form is this tree written in, and where is this session's project
dir"; every path helper here is built on it, and sibling modules
(``run_state``, ``specs_index``, ``memory``) route through it rather than
naming ``_plexus.yaml`` or an ``_``-prefixed area themselves. The tooling
verbs that need the core's API itself (``adopt``, ``migrate``) get it from
:func:`core_module`, so the core import is spelled here alone too. One module
knowing the second set of names is the point: two would drift, and a drifted
pair writes half a session's record at the legacy names and half at the
schema ones — the split tree the validator flags ERROR.
"""

import importlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Optional, TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

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
# The target classifier (run-identity spec §2, decision D2)
# ---------------------------------------------------------------------------

#: Row 3 of the §2 table — a URL naming ONE work item inside a forge
#: repository — as ``(segment, id)`` pairs. ``segment`` is the path segment as
#: both forges spell it, bare: ``issues`` reads GitLab's ``/-/issues/N`` and
#: GitHub's ``/issues/N`` through one entry, because the ``/-/`` is GitLab's
#: project/route separator and not part of the route. ``id`` is the regex the
#: NEXT segment must match, and one is always required: this is the indicator
#: set ``_derive_repo_url_from_task_target`` reads today made id-aware — the
#: id-less list pages are :data:`REFERENCE_INDICATORS`. Tickets take a number;
#: a commit or a compare range takes any one segment (``/-/commit/main`` names
#: a commit by ref and derives a repository today, so it stays a work item
#: rather than falling to ``other``). The segment is read in the forge's ROUTE
#: position only (:func:`_forge_route`: the first segment after GitLab's
#: ``/-/``, the third on GitHub's shape) — never later in the path, where
#: ``issues/123`` is a filename (astra, !380).
#:
#: The shape is chosen so ONE definition can serve its three readers: the
#: classifier (the regex below), the repository derivation (membership of a
#: path segment, exactly the test it makes on its literal tuple today) and the
#: web run card's vocabulary seam (``web/src/format.js`` ``TARGET_KINDS`` is
#: keyed by the bare segment and ``tests/test_platform_web_runcard.py`` reads
#: the Python list). Rewiring those readers is a later task: the literal
#: ``indicators = (…)`` tuples in ``lmer_cli.cli`` and ``clone_and_exec`` stay
#: in place until then.
WORK_ITEM_INDICATORS = (
    ("merge_requests", r"\d+"),
    ("issues", r"\d+"),
    ("work_items", r"\d+"),
    ("pull", r"\d+"),
    ("pulls", r"\d+"),
    ("commit", r"[^/]+"),
    ("compare", r"[^/]+"),
)

#: Row 4 — content inside a forge repository (#523, #391), read in the same
#: route position as row 3. GitLab's ``-/`` spellings are the route after
#: ``/-/``, with or without a tail, so the id-less list pages ``/-/issues``,
#: ``/-/work_items`` and ``/-/merge_requests`` land here once row 3 has passed
#: on them; the ``/-/`` is the forge's own marker, so they need no host rule.
#: GitHub's bare spellings are the route in GitHub's ``/<owner>/<repo>/<route>``
#: position AND need a known forge host (:func:`_known_http_forge`), so a
#: MediaWiki ``/wiki/`` page or a docs site's ``/a/b/tree/x`` is not read as
#: forge content and ``repository_of`` invents no clone URL for it. ``commits``
#: (GitLab ``/-/commits/<ref>``, GitHub ``/commits/<ref>``) is a reference and
#: not a work item: it names a listing the way ``/tree/`` does, and it derives
#: a repository today, so it may not fall to ``other``.
REFERENCE_INDICATORS = (
    "-/blob", "-/tree", "-/raw", "-/wikis", "-/commits",
    "-/issues", "-/work_items", "-/merge_requests",
    "blob", "tree", "wiki", "commits", "issues", "pulls",
)

#: The two sets as the classifier reads them, by route: the id regex each
#: work-item route requires of the segment after it, and the reference routes
#: by forge shape — GitLab's after its ``/-/`` (the ``-/`` prefix dropped),
#: GitHub's bare.
_WORK_ITEM_IDS = {segment: re.compile(id_re) for segment, id_re in WORK_ITEM_INDICATORS}
_GITLAB_REFERENCE_ROUTES = frozenset(
    route.removeprefix("-/") for route in REFERENCE_INDICATORS if route.startswith("-/")
)
_GITHUB_REFERENCE_ROUTES = frozenset(
    route for route in REFERENCE_INDICATORS if not route.startswith("-/")
)

#: A scheme URL (``scheme://…``) and git's scp form (``[user@]host:path``,
#: with the user required here — the userless form is indistinguishable from
#: free text with a colon in it).
_TARGET_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_TARGET_RE = re.compile(r"^(?P<user>[^/@\s]+)@(?P<host>[^/:@\s]+):(?P<path>\S*)$")

#: ``lmer_platform.spawn``'s forge rules, carried over: hosts on which a
#: suffix-less HTTP project URL is unambiguous, by allowlist or first label.
_KNOWN_HTTP_FORGES = frozenset({"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"})
_FORGE_FIRST_LABELS = frozenset({"git", "gitlab", "github", "bitbucket", "codeberg"})

#: The handlers' claims, re-encoded (``slack_chat.permalink`` and
#: ``matrix_chat.permalink``): the Slack ``p<TS>`` token needs seven
#: characters after the ``p`` (the length check the handler makes — it does
#: not test that they are digits); a Matrix room is ``!id:server`` or
#: ``#alias:server`` and an event ``$…``, read from the fragment.
_SLACK_MIN_TS_DIGITS = 7
_MATRIX_ROOM_RE = re.compile(r"^[!#][^/\s]+:[^/\s]+$")
_MATRIX_EVENT_RE = re.compile(r"^\$[^/\s?#]+$")


def _slack_thread_target(parsed) -> bool:
    """``slack_chat.permalink.is_slack_thread_url`` on an already-parsed URL."""
    if parsed.scheme != "https" or not parsed.netloc.endswith(".slack.com"):
        return False
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "archives":
        return False
    if "thread_ts" in parse_qs(parsed.query):
        return True
    if len(segments) < 3 or not segments[2].startswith("p"):
        return False
    return len(segments[2]) - 1 >= _SLACK_MIN_TS_DIGITS


def _matrix_thread_target(parsed) -> bool:
    """``matrix_chat.permalink.parse_matrix_permalink`` on an already-parsed URL:
    the matrix.to form and the Element ``https://<host>/#/room/…`` form alike."""
    if parsed.scheme not in ("https", "http") or not parsed.fragment:
        return False
    fragment = parsed.fragment.split("?", 1)[0]
    parts = [unquote(part) for part in fragment.strip("/").split("/") if part]
    if parts and parts[0] == "room":
        parts = parts[1:]
    return (
        len(parts) == 2
        and _MATRIX_ROOM_RE.match(parts[0]) is not None
        and _MATRIX_EVENT_RE.match(parts[1]) is not None
    )


def _known_http_forge(host: str) -> bool:
    """Whether *host* makes a suffix-less HTTP project URL unambiguous."""
    host = host.lower()
    return host in _KNOWN_HTTP_FORGES or host.split(".", 1)[0] in _FORGE_FIRST_LABELS


def _github_http_forge(host: str) -> bool:
    """Whether *host* uses GitHub's fixed ``owner/repo`` project root."""
    host = host.lower()
    return host == "github.com" or host.split(".", 1)[0] == "github"


def _forge_route(parsed) -> tuple[bool, str, str, list[str]] | None:
    """Split a parsed http(s) URL at its forge's route boundary — the SHAPE
    decided first, by host, then the route read where that shape puts it.

    GitHub's shape (:func:`_github_http_forge`): the project is the fixed
    ``/<owner>/<repo>`` (a ``.git`` on the repo dropped), the route the third
    segment, and everything after it the route's payload — a later ``/-/``
    included, so ``/blob/main/-/issues/123`` is a file. Elsewhere the first
    ``/-/`` is GitLab's boundary: the project is everything before it (nested
    groups included), the route the segment after it. A path with no ``/-/``
    off GitHub is read in GitHub's position too (codeberg, every Gitea, an
    unknown host). Returns ``(gitlab, project, route, payload)`` — which
    boundary was read, the project path, the route lowercased, the segments
    after it — or ``None`` for a path with no route (a project root, a bare
    ``/-/``). :func:`repository_of` reads the same split, so a filename is
    never a route for one reader and a project for the other (astra, !380).
    """
    segments = [segment for segment in parsed.path.split("/") if segment]
    if _github_http_forge(parsed.hostname or "") or "/-/" not in parsed.path:
        gitlab = False
        project = "/".join(segments[:2]).removesuffix(".git")
        segments = segments[2:]
    else:
        gitlab = True
        project, _, tail = parsed.path.partition("/-/")
        project = project.strip("/")
        segments = [segment for segment in tail.split("/") if segment]
    if not segments:
        return None
    return gitlab, project, segments[0].lower(), segments[1:]


def _repository_url_target(parsed) -> bool:
    """Row 5 for a scheme URL: ``spawn._plain_repo_url_from_target``'s rules
    with §2's deltas (``.git`` first, ``file://`` and ``git://`` accepted)."""
    if parsed.query or parsed.fragment or "/-/" in parsed.path:
        return False
    scheme = parsed.scheme.lower()
    project = parsed.path.strip("/").removesuffix(".git")
    if scheme == "file":
        return bool(project)
    if not parsed.hostname or not project:
        return False
    if scheme in ("http", "https"):
        if parsed.path.rstrip("/").endswith(".git"):
            return True
        if _github_http_forge(parsed.hostname):
            return len(project.split("/")) == 2
        return _known_http_forge(parsed.hostname)
    if scheme in ("ssh", "git"):
        return parsed.password is None
    return False


def classify_target(target: str | None) -> str:
    """Sort a task target into exactly one of six classes (spec §2, D2).

    Pure — no filesystem, no environment. The host CLI resolves local
    checkouts BEFORE calling this (a positional naming a checkout is the
    repository and never reaches here); the platform, which has no
    filesystem, classifies the raw string. Rows are tested in the table's
    order and the first match wins:

    ``none``        ``None``, ``""``, whitespace.
    ``special``     a URL a ``TargetHandler`` claims — the handlers' own
                    claims re-encoded (:func:`_slack_thread_target`: https,
                    a host ending in ``.slack.com``, ``/archives/<channel>/``
                    then either ``?thread_ts=`` or a ``p``-token;
                    :func:`_matrix_thread_target`: http or https on ANY
                    host, a fragment ``#/[room/]<!id|#alias>:<server>/
                    $<event>`` read after percent-decoding, so the Element
                    ``https://<host>/#/room/…`` form as much as matrix.to).
                    The handler owns it; never a repository.
    ``work-item``   an http(s) URL whose forge ROUTE (:func:`_forge_route`:
                    the first segment after GitLab's ``/-/``, the third on
                    GitHub's shape) is an id-bearing indicator
                    (:data:`WORK_ITEM_INDICATORS`) followed by its id.
    ``reference``   an http(s) URL whose route names content inside a forge
                    repository (:data:`REFERENCE_INDICATORS`): a GitLab
                    ``-/`` route on any host, a GitHub-position route on a
                    known forge host — #523, #391.
    ``repository``  a clone source: ``spawn._plain_repo_url_from_target``'s
                    rules carried over (the ``_KNOWN_HTTP_FORGES`` allowlist
                    and the first-label heuristic; exactly two path segments
                    on a GitHub-shaped host; no query, fragment or ``/-/``)
                    with §2's three deltas — ``.git`` is tested first, so
                    ``github.com/a/b/c.git`` is a repository; ``file:///path``
                    is accepted; ``git://`` is accepted — plus scp form
                    ``git@host:path`` (the one scp user spawn and
                    ``resolve.is_likely_url`` know), ``ssh://host/path``,
                    and anything beginning with ``/`` or ``.`` (a path; git
                    refuses ref names starting with either, so no branch is
                    lost).
    ``other``       any other URL: a non-forge host, a forge URL with a
                    query or an unknown route (``/discussions/5``,
                    ``/-/pipelines/1691``), a GitHub-position reference
                    route off the forges (``docs.example.com/a/b/tree/x``),
                    an unsupported transport.
    ``work-item``   anything else — a commit SHA, a branch name, an
                    ``mr-N``/``pr-N``/``issue-N`` shorthand, free text.

    Decisions recorded here (A1-wave0.classifier):

    - The indicator constant's shape: ``(segment, id)`` pairs with bare
      segments and an id always required — see :data:`WORK_ITEM_INDICATORS`.
    - GitHub ``/commits/<ref>`` joins the reference row beside GitLab's
      ``/-/commits/`` — see :data:`REFERENCE_INDICATORS`.
    - An embedded credential on a non-HTTP transport
      (``ssh://user:secret@host/path``, ``user:secret@host:path``) is
      ``other``: spawn refuses it as a plain repository and the container's
      no-secrets rule (``sources.url_has_embedded_credential``) refuses it at
      start, so it never becomes a repository of record and — ``other``
      never reaching a slug — never a directory name either. An HTTP(S)
      credential is transport material spawn already scrubs, so that URL
      stays a repository; the scrub is the consumer's job, not this one's.
    - A scp-form string with any user but ``git`` (``deploy@host:path``) or
      with no path (``git@host:``) is ``other`` as well: spawn and
      ``is_likely_url`` agree it is not a repository URL, and it is not a
      work item.
    - The route decides, by position (astra, !380): the path is split at the
      forge's route boundary — GitLab's ``/-/``, or GitHub's fixed
      ``/<owner>/<repo>/<route>`` — and only that segment, with the id rule
      on the one after it, sorts work item from reference. Nothing later in
      the path can: ``/blob/main/issues/123`` and
      ``/-/blob/main/compare/result.md`` name files and are references, not
      work items sharing a group with the issue they happen to spell.
    - The shape is decided before the route, by host (round two): on a
      GitHub-shaped host the route is the third segment and a later ``/-/``
      is payload — ``/blob/main/-/issues/123`` is a file, not GitLab's
      boundary — while elsewhere the FIRST ``/-/`` is that boundary, so a
      nested group ``/g/sub/p/-/issues/1`` keeps its whole project and
      ``/-/blob/main/-/issues/123`` still reads ``blob``. :func:`repository_of`
      derives from the same split (:func:`_forge_route`), so no URL is a
      reference here and a nonexistent repository there.
    - A GitHub-position reference route (``tree``, ``blob``, ``wiki``,
      ``commits``, the ``issues``/``pulls`` list pages) reads as forge
      content on a known forge host only (:func:`_known_http_forge`), so
      ``https://docs.example.com/a/b/tree/x`` is ``other`` and
      :func:`repository_of` never invents a clone URL for a docs site.
      GitLab's ``/-/`` is the forge's own marker and needs no host rule. An
      id-bearing route in GitHub's position stays host-agnostic — codeberg
      and every Gitea spell ``/o/r/issues/5`` that way, and an unknown
      host's ``/pull/9`` derived a repository before this rule — so
      ``https://x/org/p/pull/9`` is still a work item.

    Mirrored verbatim in ``lmer_cli/container/clone_and_exec.py``, which runs
    without this package; ``tests/test_work_repo_classify_target.py`` holds
    the two copies AST-identical and runs the §2 table against both.
    """
    text = (target or "").strip()
    if not text:
        return "none"
    try:
        parsed = urlparse(text)
    except ValueError:
        parsed = None
    if parsed is not None and (_slack_thread_target(parsed) or _matrix_thread_target(parsed)):
        return "special"
    if _TARGET_URL_RE.match(text):
        if parsed is None:
            return "other"
        if parsed.scheme.lower() in ("http", "https"):
            split = _forge_route(parsed)
            if split is not None:
                gitlab, _, name, payload = split
                id_re = _WORK_ITEM_IDS.get(name)
                if id_re is not None and payload and id_re.fullmatch(payload[0]):
                    return "work-item"
                if gitlab and name in _GITLAB_REFERENCE_ROUTES:
                    return "reference"
                if (
                    not gitlab
                    and name in _GITHUB_REFERENCE_ROUTES
                    and _known_http_forge(parsed.hostname or "")
                ):
                    return "reference"
        return "repository" if _repository_url_target(parsed) else "other"
    scp = _SCP_TARGET_RE.match(text)
    if scp is not None:
        if scp.group("user") == "git" and scp.group("path"):
            return "repository"
        return "other"
    if text.startswith(("/", ".")):
        return "repository"
    return "work-item"


def is_work_item(target: str | None) -> bool:
    """Whether *target* names a work unit: ``work-item`` or ``special`` (§2).

    A thread permalink counts — the handler-owned chat IS the work, and its
    slug is derived from the permalink as today.
    """
    return classify_target(target) in ("work-item", "special")


def repository_of(url: str | None) -> str | None:
    """The repository a ``work-item``, ``reference`` or ``repository`` URL
    belongs to, as a URL — ``None`` when there is none (spec §2).

    One answer behind every reader of "which repository is this": the clone
    URL (``_derive_repo_url_from_task_target``, both copies), the identity
    ``LMER_REPO_HOST``/``LMER_REPO_PROJECT`` are parsed from
    (``lmer_cli.cli._parse_repo_url`` parses ``repository_of(url) or url``)
    and, in A2, the platform's repository of record and its identity
    prediction. So ``LMER_REPO_PROJECT`` never contains a ``/-/`` route or a
    ``/pull/N`` tail (#391), and a document URL names the repository its
    clone comes from (#523). By class (:func:`classify_target`):

    ``work-item``, ``reference``
        A derived answer, ``<scheme>://<host[:port]>/<project>`` with the
        project the classifier's own split (:func:`_forge_route`, shape
        first): on a GitHub-shaped host the fixed ``<owner>/<repo>``, a
        ``.git`` on the repo segment dropped (what the derivation did) and
        a later ``/-/`` being payload; elsewhere the path before the first
        ``/-/``, nested groups included.
        The host and port are the target's — the port is part of the
        address; userinfo is NOT carried over: this answer is read into run
        identities and platform logs, and a credential in a work-item link
        is transport material that belongs to whoever clones. The query and
        the fragment go with the route. A non-URL work item (a SHA, a
        branch, ``mr-7``, free text) names no repository: ``None``. So does
        a route with no project before it (``https://host/-/issues/5``).
    ``repository``
        Itself, as given (whitespace aside): the clone source in the
        spelling the caller chose, a path included. A credential embedded
        in it is the consumer's to scrub, as the classifier records.
    ``none``, ``special``, ``other``
        ``None`` — a thread permalink is the handler's; ``other`` is context
        only when ``--repo`` names the repository.

    Never adds a credential: no token lookup, no environment. Pure, like the
    classifier, and mirrored with it in ``lmer_cli/container/clone_and_exec.py``
    under the same AST guard.
    """
    text = (url or "").strip()
    klass = classify_target(text)
    if klass == "repository":
        return text
    if klass not in ("work-item", "reference") or not _TARGET_URL_RE.match(text):
        return None
    parsed = urlparse(text)
    netloc = parsed.netloc.rpartition("@")[2]
    # The classifier's own split: the project is whatever sits before the
    # route it read, never a second guess at the path.
    split = _forge_route(parsed)
    if not netloc or split is None or not split[1]:
        return None
    return f"{parsed.scheme}://{netloc}/{split[1]}"


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


#: The core API's package, spelled here and nowhere else in the work CLI
#: (see :func:`core_module`).
CORE_PACKAGE = "lmer_schema"


class CoreUnavailableError(ImportError):
    """The lmer-schema core API cannot be imported in this build.

    The ONE error :func:`core_module` raises. An ``ImportError`` so that
    ``str(exc)`` is the loader's own text — the version-floor refusals in
    ``adopt``/``migrate`` quote it verbatim — and so that an
    ``except ImportError`` written for a direct core import still catches
    it. Not :class:`SchemaContextError`: that one is about the TREE (a
    manifest this writer cannot safely use); this one is about the BUILD.
    """


def core_module(name: str) -> ModuleType:
    """Import and return ``lmer_schema.<name>`` — THE core import for tooling.

    The tooling verbs — ``adopt``, ``migrate``, and later ``housekeep`` and
    ``knowledge`` — reach the core ONLY through this accessor
    (``core_module("writer").load_for_write``, ``core_module("checks.walk")``,
    *name* a dotted path under the package) and never spell the core's
    import or the manifest's name themselves; one module knowing the second
    set of names is the point (D20, and the source guard in
    tests/test_work_repo_schema_base.py pins it). The accessor is an import
    and nothing more: it never reads the manifest and never decides the
    tree's form — that stays :func:`work_repo_layout` / :func:`project_area`.

    Raises :class:`CoreUnavailableError` — one type, whatever failed — when
    the core is absent, half-installed, or *name* is not a module of it. The
    same reading :func:`_core_missing` gives a direct import: an
    ``ImportError`` cannot tell "no core" from "a core with something broken
    under it", and the accessor does not try; the message and the chained
    cause carry the loader's detail. Callers that must never raise (a
    journal seam, a hook path) catch it and fall back, exactly as they did
    for ``ImportError``; mutating verbs turn it into their own refusal.

    Lazy by design: call it inside the verb, never at module level, so
    ``import work_repo.adopt`` still succeeds on a build without the core.
    """
    try:
        return importlib.import_module(f"{CORE_PACKAGE}.{name}")
    except ImportError as exc:
        raise CoreUnavailableError(str(exc), name=exc.name, path=exc.path) from exc


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
