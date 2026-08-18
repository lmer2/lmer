"""Canonical source config (``{work_repo}/sources.yaml``) — container-side.

Owns the schema-1 parse/validate/resolve logic for declared taskdef/napkin
sources (spec: canonical-source-config, issue #105). Resolution runs
inside ``clone_and_exec.py`` after the work-repo clone and before the
aux clones, so this module must be loadable in that standalone context.
The module carries the import contract, error taxonomy, and PyYAML guard
plus the core API: the pure URL normalizer, schema-1 parse/validation
(``load_sources``), credential derivation, and redaction helpers.

Import contract (decision record)
=================================
``clone_and_exec.py`` runs standalone (``python3 .../clone_and_exec.py``,
see its module docstring) and must not import from the ``lmer_cli`` package
— it executes before the package is guaranteed importable. Three loading
mechanisms were considered for pulling this sibling file in:

(a) REJECTED — implicit ``sys.path[0]``: because the host launches the
    script by path, CPython normally prepends the script's directory to
    ``sys.path``, so a bare ``import sources`` works. But that prepend is
    exactly what ``PYTHONSAFEPATH``/``-P`` disables (the host CLI already
    runs children with ``-P`` — see ``cli.py`` ``_spawn_clone_cache_update``)
    so the import silently breaks the moment anyone hardens the container
    invocation the same way; and it squats the very generic top-level name
    ``sources`` in ``sys.modules`` for the whole process.

(b) REJECTED — ``sys.path.insert(0, str(Path(__file__).resolve().parent))``
    (the ``hooks/followup.py`` precedent): survives ``-P``, but is a
    process-wide namespace mutation serving a single import — every sibling
    module (``taskdefs``, ``masterplan``, ``dispatch_agents``, …) becomes
    importable under a bare top-level name for the rest of the process, and
    the module still lands in ``sys.modules`` as generic ``sources``. There
    is no ``sys.path``-mutation precedent inside ``src/`` today; not adding
    one.

(c) CHOSEN — ``importlib.util.spec_from_file_location`` on the sibling file
    under the namespaced module name ``lmer_container_sources``: touches
    neither ``sys.path`` nor any generic name, cannot be shadowed by a stray
    ``sources.py`` in the cwd, and is indifferent to ``-P``. The only cost
    is a few lines of loader boilerplate, recorded below.

Loader recipe (copy-paste into clone_and_exec's resolution subsystem)::

    import importlib.util
    import sys
    from pathlib import Path

    def _load_sources_module():
        # Load the sibling sources.py without importing lmer_cli.
        name = "lmer_container_sources"
        if name in sys.modules:
            return sys.modules[name]
        path = Path(__file__).resolve().parent / "sources.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

Dual-import caveat: when the ``lmer_cli`` package *is* importable (host CLI,
tests), ``from lmer_cli.container import sources`` loads this same file a
second time under its package name. The two module objects have distinct
class identities (``except lmer_container_sources.SourcesConfigError`` will
not catch the package copy's exception and vice versa). Do not mix the two
load paths within one consumer: ``clone_and_exec`` uses only the standalone
copy; everything else uses only the package import.

Error taxonomy
==============
- ``SourcesConfigError`` — the single refuse-start exception type. Raised
  for any condition where a present ``sources.yaml`` cannot be trusted or
  read (unreadable/unparseable file, unsupported schema version, embedded
  credentials, a declared URL with two incompatible readings (see
  ``_url_shape_is_ambiguous``), cross-host trust-rule violation, PyYAML
  missing while the file exists). The caller catches it and refuses start
  (headless: exit 2) — a present config is never silently ignored.
- Warnings channel — recoverable findings (unknown keys, forward-compat
  notices) never raise; loaders return them as a ``list[str]`` alongside
  the parsed config, and the caller decides how to surface them.

PyYAML guard (spec N5)
======================
``import yaml`` is guarded so this module imports cleanly when PyYAML is
absent (``yaml`` is then ``None``). Parsing an *existing* ``sources.yaml``
without PyYAML raises ``SourcesConfigError`` loudly — never a silent skip.
An absent file is silent legacy mode and never needs PyYAML.

Doctor CLI (frozen seam)
========================
This module doubles as the doctor subsystem's parse/normalize/derive tool
so ``bin/doctor`` consumes this code instead of reimplementing URL handling
in sh. The surface below is a quote-verbatim contract for that consumer —
changing any of it is a breaking change to doctor:

- Module path ``lmer_cli.container.sources``; subcommands
  ``validate | normalize | derive | doctor``; canonical invocation::

      "${LMER_PYTHON:-python3}" -m lmer_cli.container.sources doctor --json [--emit-clone-urls]

- Output: exactly ONE JSON document on stdout; human-readable text
  (warnings, errors, usage messages) goes to stderr only. Every document
  carries ``ok`` (bool) and ``errors`` (list). ``--json`` is accepted on
  every subcommand as the explicit marker for this always-on behavior.
- Exit codes: ``0`` ok; ``2`` validation refusal (the SourcesConfigError
  refuse-start taxonomy above); ``64`` usage error (sysexits EX_USAGE —
  distinct from the refusal code on purpose).
- Redaction: every URL in the JSON passes through ``scrub_credentials``,
  with ONE documented opt-in exception — the ``clone_url`` field emitted
  by ``derive``/``doctor`` under ``--emit-clone-urls``. Default output is
  fully redacted; the sibling ``clone_url_redacted`` flag records which
  mode produced the document.
- Subcommands:

  * ``validate PATH [--work-repo-url URL]`` — run ``load_sources`` on the
    file; emit declared sources, warnings, errors.
  * ``normalize URL`` — emit the normalized comparison form
    (``host/path``, non-default port kept), the trust-rule host (hostname
    only, no port), and the embedded-credential verdict.
  * ``derive DECLARED_URL --work-repo-url URL [--emit-clone-urls]`` —
    emit the credential-derived clone URL and derivation ``mode``, with
    the anonymous marker surfaced as ``anonymous``.
  * ``doctor [--path PATH] [--work-repo-url URL] [--emit-clone-urls]`` —
    aggregated machine-readable check document: declared sources with
    derived clone URLs, warnings, errors, ``supported_sources_schemas``
    and ``supported_taskdef_schemas``.

- Derivation mode ``https-credential-file`` means the emitted clone URL is
  clean and the consumer may authenticate it with the in-container
  ``LMER_WORK_REPO_CREDENTIAL_FILE``. The explicit ``--work-repo-url`` path
  never borrows that ambient file.

  ``--work-repo-url`` falls back to ``$LMER_REPO_URL`` (the container's
  work-repo URL variable; doctor prefers ``$LMER_WORK_REPO`` before the
  legacy ``$LMER_REPO_URL`` fallback); ``doctor --path`` defaults to
  ``${LMER_WORK_REPO_PATH:-/work}/sources.yaml`` — the work-repo clone, the
  same location clone_and_exec resolves the declaration from.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - exercised via sys.modules block in tests
    yaml = None

# The sys.modules key clone_and_exec registers the standalone copy under
# (see the loader recipe in the module docstring). Namespaced so it cannot
# collide with a dependency or a stray cwd module named "sources".
STANDALONE_MODULE_NAME = "lmer_container_sources"

# Filename of the declaration at the work-repo root.
SOURCES_FILENAME = "sources.yaml"

# Schema versions load_sources understands. Unknown versions fail loud in
# the taskdef.yaml pattern (hooks/start.py read_taskdef_schema) — a present
# config this loader cannot interpret must never be silently ignored.
SUPPORTED_SOURCES_SCHEMAS = (1,)

# Taskdef schema versions the start-hook renderer understands, surfaced in
# the `doctor` document. MIRRORED from hooks/start.py SUPPORTED_TASKDEF_SCHEMAS
# as a module-local constant — NEVER import hooks/start.py here: it imports
# yaml and jinja2 at module scope, which would break this module's
# import-cleanly-without-PyYAML contract. tests/test_start_hook.py carries a
# drift-guard test (same pattern as the _is_github_host guard) that fails
# when the two constants diverge.
SUPPORTED_TASKDEF_SCHEMAS = (1, 2)

# Doctor CLI exit codes (frozen seam, see module docstring): ok / validation
# refusal / usage error must stay distinct so the sh caller can tell "config
# is bad" apart from "I called the tool wrong". 2 matches the refuse-start
# taxonomy (headless exit 2); 64 is sysexits EX_USAGE.
EXIT_OK = 0
EXIT_REFUSAL = 2
EXIT_USAGE = 64

# Fallback work-repo clone path, matching clone_and_exec.main() and
# work_repo.utils: the work repo lives at $LMER_WORK_REPO_PATH, /work when
# unset. NOT /workspace — that is the target-repo checkout.
_DEFAULT_WORK_REPO_PATH = "/work"

# Source keys schema 1 supports, and the env var that overrides each one
# (named in the cross-host trust-rule error as the sanctioned escape hatch).
SOURCE_ENV_OVERRIDES = {
    "taskdef": "LMER_TASKDEF_REPO",
    "napkin": "LMER_NAPKIN_REPO",
}

# Reserved-for-later keys under `sources:` — warned about by name so the
# operator learns the key is known-but-unsupported, not a typo.
_RESERVED_SOURCE_KEYS = ("masterplan_mirror",)

# Credential-derivation modes returned by derive_clone_url (second tuple
# element). ANONYMOUS is the explicit marker for the credential-less
# work-repo case: the caller uses it to drive the loud declared-source
# failure path when the anonymous clone attempt fails (spec N2/N3).
# ANONYMOUS_OTHER_PORT is the second credential-less marker (review
# finding): the work repo DOES carry a credential, but the declared URL
# names a different port on the same host, so the credential is withheld
# rather than sent to a service the work-repo clone never authenticated
# against. Both drive the same anonymous-attempt semantics; they differ
# only in the reason the caller reports. ANONYMOUS_MODES is the tuple
# every consumer tests against — a bare `== DERIVED_ANONYMOUS` check
# would silently treat the withheld-credential case as credentialed.
DERIVED_HTTPS_USERINFO = "https-userinfo"
DERIVED_HTTPS_CREDENTIAL_FILE = "https-credential-file"
DERIVED_SSH = "ssh"
DERIVED_ANONYMOUS = "anonymous"
DERIVED_ANONYMOUS_OTHER_PORT = "anonymous-other-port"
ANONYMOUS_MODES = (DERIVED_ANONYMOUS, DERIVED_ANONYMOUS_OTHER_PORT)

# Effective ports assumed for a scheme whose URL names none, used by
# derive_clone_url to compare the declared authority against the work
# repo's. Distinct from _DEFAULT_PORTS (the scheme-blind equality helper
# for normalize_repo_url): here the scheme is known and the question is
# "would git connect to the same port?", so http's 80 belongs in it and
# scheme-blindness would be wrong.
_SCHEME_PORTS = {"http": "80", "https": "443", "ssh": "22"}

# Ports dropped by normalize_repo_url so `https://host:443/x` and
# `https://host/x` (and ssh :22) compare equal. Deliberately scheme-blind:
# both ports are dropped whatever the scheme (`https://host:22/x` also
# normalizes bare) — slightly broader than per-scheme defaults, and
# accepted: a cross-scheme default-port URL is not a realistic git remote.
# The trust-rule key (repo_url_host) drops the port outright and so does
# not consult this.
_DEFAULT_PORTS = ("443", "22")

# scheme:// detector (RFC 3986 scheme charset); anything else is treated as
# scp-form (`user@host:path`) or an opaque path-like string.
_SCHEME_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://")

# Redaction (see scrub_credentials): the first pattern is the
# clone_and_exec._scrub_credentials regex (userinfo in scheme URLs); the
# second extends it to scp-form `user:secret@host:path` credentials, which
# have no `://` anchor. Both are simple substitutions that cannot raise.
#
# `[^\s@]*` rather than `[^\s@/]*`: a secret may contain "/" (the base64
# alphabet has one), which would otherwise end the match before the "@" and
# leave the credential in the output — redaction must fail closed (review
# finding, matching _userinfo_boundary's extension). _SCHEME_CRED_RE stays
# for the no-secret `ssh://git@host/x` form, which has no ":" to anchor on;
# the scp pattern refuses a `//` after the colon so it never re-reads a
# scheme URL's own `scheme://` prefix.
#
# The widening does over-scrub one exotic shape — a URL with BOTH a
# non-default port and an "@" in its path (`https://host:8443/a/repo@v1.git`)
# reads as `host:<secret>@` and is redacted down to `https://v1.git`. Left
# that way on purpose: distinguishing it needs a port-vs-secret heuristic
# whose failure mode is a leaked numeric secret, and this docstring's rule
# is that over-scrubbing is fine and leaking is not.
_SCHEME_CRED_RE = re.compile(r"(://)[^/\s]*@")
_SCHEME_CRED_SECRET_RE = re.compile(r"(://)[^\s@/]*:[^\s@]*@")
_SCP_CRED_RE = re.compile(r"\b[\w.+-]+:(?!//)[^\s@]*@")

# Refusal guidance for the ambiguous scheme-less shape (see
# _url_shape_is_ambiguous). Deliberately echoes NO part of the URL: under one
# reading the string carries a secret, and under the other scrub_credentials
# over-scrubs it down to a fragment that would misname the source. Both
# readings are addressed so the operator is never sent to the wrong place.
_AMBIGUOUS_SHAPE_REFUSAL = (
    "it has no scheme and an `@` after a `/`, so `a:b/c@d` reads either as "
    "userinfo whose secret contains a `/` or as an scp-form path containing "
    "an `@`. Refusing rather than guessing. If it carries a credential, "
    "strip it: the work repo is shared, never commit tokens, and auth is "
    "derived from the work-repo URL at clone time. If the `@` belongs to the "
    "repo path, declare the scheme form (`https://host/org/re@po.git`), "
    "which parses unambiguously."
)


class SourcesConfigError(Exception):
    """Refuse-start error: a present sources.yaml cannot be read or trusted.

    The single exception type in this module's taxonomy — callers treat any
    instance as "do not start the session" (headless: exit 2). Recoverable
    findings go through the warnings-list return channel instead.
    """


def _userinfo_boundary(text, scp_form: bool = False) -> int:
    """Index of the ``@`` separating userinfo from host, or ``-1`` if none.

    The boundary is the last ``@`` before the first path separator — RFC
    3986's authority rule, and what git's own URL parser implements.

    One deliberate extension: when no ``@`` precedes the first separator but
    a later one does, and the text before that ``@`` has ``user:secret``
    shape, the later ``@`` is taken as the boundary instead. Such a URL is
    malformed — a raw ``/`` in userinfo must be percent-encoded and git will
    not clone it — but the credential in it is real, so the no-secrets
    refusal and the redactor have to see it rather than fail open and print
    the secret (review finding; base64-alphabet tokens routinely carry ``/``).

    ``user:secret`` shape means a ``:`` in the candidate's first segment
    whose right-hand side is not a bare port number — that last clause is
    what keeps ``host:8080/org/repo@v1.git`` parsing as a ported host with an
    ``@`` in its path rather than as userinfo.

    *scp_form* adds the discriminator that shape guard cannot supply for
    scp-form input (review finding): there the host/path colon is mandatory
    and its right-hand side is the first path segment, never digits, so the
    digit test always passes and the extension always fires — misreading
    the userless ``git.example.com:org/re@po.git`` as userinfo with host
    ``po.git``, then refusing it as a credential that does not exist. Since
    scp-form is ``[user@]host:path``, the text AFTER a real boundary must
    itself carry a ``:``; requiring that keeps ``user:se/cret@host:org/x.git``
    (after-``@`` is ``host:org/x.git``) recognized while leaving an ``@``
    that only appears in the path alone. Scheme-form passes the flag
    False — its host segment carries a colon only for a port, which the
    digit guard already covers.

    Where the two readings disagree the input is genuinely ambiguous — see
    _url_shape_is_ambiguous, which is what callers refuse on. This helper
    deliberately does NOT grow a fourth guard to pick a winner: there is no
    syntactic one to pick (``a:b/c@d`` is the same string either way).
    """
    slash = text.find("/")
    at = text.rfind("@", 0, slash if slash != -1 else len(text))
    if at == -1 and "@" in text:
        candidate = text.rpartition("@")[0]
        _, colon, secret = candidate.partition("/")[0].partition(":")
        if colon and not secret.isdigit():
            if not scp_form or ":" in text[len(candidate) + 1:]:
                at = len(candidate)
    return at


def _parse_repo_url(url) -> Tuple[str, str, str, str, str]:
    """Split a git URL into ``(scheme, userinfo, host, port, path)``.

    Pure string dissection shared by the normalizer, credential detection,
    and derivation — no I/O, no env reads, never raises. Handles both
    scheme URLs (``https://user:tok@host:port/path``,
    ``ssh://git@host/path``) and scp-form (``git@host:path``,
    ``user:secret@host:path``). ``scheme`` is lowercased and empty for
    scp-form; ``path`` carries no leading separator for scheme URLs and is
    otherwise verbatim (case preserved, ``.git`` kept). A string with
    neither scheme nor userinfo comes back entirely in ``path``.
    """
    url = (url or "").strip()
    m = _SCHEME_RE.match(url)
    if m:
        # Userinfo first, then the authority/path split — doing it the other
        # way round hides a userinfo whose secret contains "/" inside `path`
        # (see _userinfo_boundary).
        rest = url[m.end():]
        at = _userinfo_boundary(rest)
        userinfo, rest = (rest[:at], rest[at + 1:]) if at != -1 else ("", rest)
        netloc, _, path = rest.partition("/")
        host, _, port = netloc.partition(":")
        return m.group("scheme").lower(), userinfo, host, port, path
    # scp-form: userinfo@host:path — same boundary rule, so `user:secret@host:x`
    # parses with the whole `user:secret` as userinfo, not just `user`; the
    # scp_form flag keeps a path-only `@` from being read as one.
    at = _userinfo_boundary(url, scp_form=True)
    if at != -1:
        host, _, path = url[at + 1:].partition(":")
        return "", url[:at], host, "", path
    return "", "", "", "", url


def _url_shape_is_ambiguous(url) -> bool:
    """True when a scheme-less URL has two incompatible readings.

    ``a:b/c@d`` is the shape: `git.example.com:org/re@po.git` is an scp URL
    whose PATH contains an "@", and `user:se/cret@git.example.com/org/x.git`
    is userinfo whose SECRET contains a "/". They are the same string shape,
    so no parser rule can tell them apart — the iteration-4 and iteration-5
    review findings are the two halves of that one ambiguity, each reporting
    the reading the other needs.

    Rather than a fourth guard on _userinfo_boundary (which would just move
    the wrong answer around), the disagreement is detected and named: the
    ungated boundary is the credential reading, the scp-gated one is the
    path-``@`` reading, and where they differ callers refuse with a message
    that covers BOTH — the operator knows which one it is, and neither the
    "strip a credential that is not there" nor the "wrong place to look"
    complaint can happen. Redaction is independent of this (scrub_credentials
    is a regex that fails closed on both readings), so a refusal message and
    any diagnostic output stay safe whichever reading is true.

    Scheme URLs are never ambiguous: ``://`` anchors the authority.
    """
    url = (url or "").strip()
    if _SCHEME_RE.match(url):
        return False
    return _userinfo_boundary(url) != _userinfo_boundary(url, scp_form=True)


def _effective_port(scheme, port) -> str:
    """Port git would actually connect to for *scheme*/*port*.

    An explicit port wins; otherwise the scheme's default (see
    _SCHEME_PORTS). An unknown scheme with no port has no answer and
    returns "" — derive_clone_url only calls this for http(s) URLs, where
    a default always exists.
    """
    return port or _SCHEME_PORTS.get(scheme, "")


def normalize_repo_url(url) -> str:
    """Canonical comparison form of a git repo URL: ``host/path``.

    Pure function (no I/O, no env reads, no credential injection — the
    tokens.py converter is deliberately NOT reused, spec revision note).
    Strips userinfo/credentials, lowercases the host (path case preserved),
    drops the default ports :443/:22, strips a trailing ``.git`` and
    trailing slash, and folds scp-form ``git@host:path`` and
    ``ssh://git@host/path`` into the same ``host/path`` form as
    ``https://host/path``. Non-default ports are kept as ``host:port``.
    """
    _, _, host, port, path = _parse_repo_url(url)
    host = host.lower()
    if port and port not in _DEFAULT_PORTS:
        host = f"{host}:{port}"
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    if host and path:
        return f"{host}/{path}"
    return host or path


def repo_url_host(url) -> str:
    """Normalized host component of a git URL (trust-rule comparison key).

    Lowercased hostname only — the **port does not participate** (review
    finding). A port-sensitive key rejects a valid and common self-hosted
    layout: an SSH work repo on ``:2222`` alongside HTTPS declarations on
    443, which ``derive_clone_url`` reconciles correctly. It buys nothing
    in exchange, because the trust rule exists to stop an agent-writable
    ``sources.yaml`` routing the operator's credential to *another host* —
    a second port on the host that already holds the credential is not
    that. This is also what ``docs/LMER-CLI.md`` has always documented the
    rule as ("same **host**").

    ``normalize_repo_url`` is the other comparison key and deliberately
    still keeps non-default ports: it answers "are these the same repo?",
    where a different port really can mean a different repo.

    Empty string when the value carries no recognizable host (bare paths).
    """
    _, _, host, _, _ = _parse_repo_url(url)
    return host.lower()


def url_has_embedded_credential(url) -> bool:
    """True when the URL's userinfo carries a secret component.

    The no-secrets rule (spec §1): userinfo with a ``:`` separates a
    user from a password/token (``user:secret@``, ``oauth2:glpat-...@``)
    and is a refuse-start error. Bare SSH userinfo — ``git@host:path``,
    ``ssh://git@host/path`` — is protocol plumbing, not a credential,
    and stays legal.
    """
    _, userinfo, _, _, _ = _parse_repo_url(url)
    return ":" in userinfo


def scrub_credentials(text) -> str:
    """Render *text* with any embedded URL credentials removed.

    Follows the clone_and_exec._scrub_credentials pattern (a regex that
    cannot raise, safe to wrap any error string with) and extends it to
    scp-form ``user:secret@host:path`` credentials, which lack the ``://``
    anchor. Over-scrubbing is fine (bare ``ssh://git@`` userinfo is also
    dropped); leaking is not.
    """
    if not text:
        return text
    text = _SCHEME_CRED_RE.sub(r"\1", text)
    text = _SCHEME_CRED_SECRET_RE.sub(r"\1", text)
    return _SCP_CRED_RE.sub("", text)


def derive_clone_url(
    declared_url, work_repo_url, *, work_repo_has_credential_file=False
) -> Tuple[str, str]:
    """Borrow the work-repo URL's auth for a declared same-host source.

    Returns ``(clone_url, mode)`` (spec N2/N3, one branch per work-repo
    auth form):

    - ``DERIVED_HTTPS_USERINFO`` — the work-repo URL is HTTP(S) with
      userinfo: that userinfo is copied onto the declared URL (an scp/ssh
      declared spelling is rebuilt on the work repo's scheme so the
      credential actually applies).
    - ``DERIVED_HTTPS_CREDENTIAL_FILE`` — doctor received the clean work-repo
      URL plus lmer's session credential-file path. The declared clone URL is
      rebuilt on the same HTTPS authority without userinfo; doctor attaches
      the helper file to its one Git process instead.
    - ``DERIVED_SSH`` — the work-repo URL is SSH-form (``git@host:...`` or
      ``ssh://git@host/...``, the REPO_AUTH_PREFER_SSH / no-token case):
      the declared URL is converted into the identical SSH form (the
      work-repo clone just proved key auth works).
    - ``DERIVED_ANONYMOUS`` — credential-less HTTP(S) work-repo URL: the
      declared URL is returned unchanged for an anonymous attempt; the
      marker lets the caller drive the loud declared-source failure path
      instead of the aux-clone warn-and-continue.
    - ``DERIVED_ANONYMOUS_OTHER_PORT`` — the work repo carries an HTTP(S)
      credential but the declared URL names a different effective port on
      that host: the declared URL is returned unchanged, credential
      withheld (review finding). The trust rule compares hostnames only,
      so "same host" now admits `https://host:5050/…` — a registry, an
      app, anything a co-tenant can bind — and this branch is the one
      that would otherwise carry `oauth2:<token>` there as basic auth.
      Withholding rather than refusing keeps a genuinely public repo on
      another port usable, and rather than rewriting the authority to the
      work repo's, which would silently clone a *different* URL than the
      one declared. An operator who really needs a credential on another
      port sets the source's env var override, which is theirs to set.

    Pure derivation — the trust rule (same host) is load_sources' job, so
    this never validates hosts and never logs; callers must render any
    derived URL through scrub_credentials before printing.
    """
    w_scheme, w_userinfo, w_host, w_port, _ = _parse_repo_url(work_repo_url)
    d_scheme, _, d_host, d_port, d_path = _parse_repo_url(declared_url)
    host = d_host or w_host
    if w_scheme == "ssh" or (not w_scheme and w_userinfo):
        user = w_userinfo or "git"
        path = d_path.lstrip("/")
        if w_scheme == "ssh":
            port = f":{w_port}" if w_port else ""
            return f"ssh://{user}@{host}{port}/{path}", DERIVED_SSH
        return f"{user}@{host}:{path}", DERIVED_SSH
    if w_userinfo or work_repo_has_credential_file:
        w_netloc = f"{host}:{w_port}" if w_port else host
        if d_scheme in ("http", "https"):
            if _effective_port(d_scheme, d_port) != _effective_port(w_scheme, w_port):
                # Same host, different port: the work-repo credential was
                # never proven against that service, so it is withheld and
                # the declared URL is attempted anonymously.
                return declared_url, DERIVED_ANONYMOUS_OTHER_PORT
            netloc = f"{d_host}:{d_port}" if d_port else d_host
            if work_repo_has_credential_file and not w_userinfo:
                return (
                    f"{d_scheme}://{netloc}/{d_path}",
                    DERIVED_HTTPS_CREDENTIAL_FILE,
                )
            return (
                f"{d_scheme}://{w_userinfo}@{netloc}/{d_path}",
                DERIVED_HTTPS_USERINFO,
            )
        # Declared in scp/ssh form while the work-repo auth is HTTPS
        # userinfo: rebuild on the work repo's scheme so the credential
        # travels with a URL git will actually send it to, on the work
        # repo's authority. An scp-form declaration asserts no port, so
        # there is nothing to reconcile; a declared `ssh://host:2222/…`
        # does name one and has it replaced here rather than withheld like
        # the http(s) branch above (review nit). The asymmetry is
        # deliberate: the credential still only ever reaches the work
        # repo's own host and port — the service it was proven against —
        # whereas the http(s) branch's other-port case would send it to a
        # service that never saw it. Converting ssh:2222 to https is
        # already a scheme change the declaration cannot dictate.
        path = d_path.lstrip("/")
        if work_repo_has_credential_file and not w_userinfo:
            return (
                f"{w_scheme}://{w_netloc}/{path}",
                DERIVED_HTTPS_CREDENTIAL_FILE,
            )
        return f"{w_scheme}://{w_userinfo}@{w_netloc}/{path}", DERIVED_HTTPS_USERINFO
    return declared_url, DERIVED_ANONYMOUS


def env_matches_declared(env_repo, declared_repo, work_repo_url=None) -> bool:
    """True when *env_repo* names the same source as *declared_repo*.

    The row-2 predicate of the resolution matrix, shared by the container
    (clone_and_exec's matrix) and the host (``--show-env``) so both surfaces
    label the same state the same way. Two chances, because the question
    row 2 actually asks is not "are these strings the same repo?" but "is
    this env value the credentialed form of this declaration?":

    1. ``normalize_repo_url`` on both — folds credentials, ``.git``,
       scp-form and ``ssh://`` spellings, so the ordinary
       env-alongside-declaration case compares equal outright.
    2. ``normalize_repo_url`` of the env value against the normalized
       ``derive_clone_url(declared_repo, work_repo_url)`` — the exact URL
       this code would clone the declaration from. That is what makes a
       work repo on a non-default SSH port work (review finding): with
       ``ssh://git@host:2222/org/work.git`` as the work repo, an operator
       whose ``LMER_TASKDEF_REPO`` holds the working ``ssh://git@host:2222/…``
       form while sources.yaml declares the clean ``https://host/…`` URL
       used to land on the mismatch row — prompt interactively, exit 2
       headless — because ``normalize_repo_url`` keeps non-default ports.
       It keeps them deliberately: ``host:2222/x`` and ``host:9999/x`` can
       genuinely be different repos, so the equality key must not drop
       them. Comparing against the derived URL instead folds exactly the
       scheme/port rewrite the derivation itself performs, and nothing
       else.

    *work_repo_url* is required for chance 2; without it (callers with no
    work-repo context) only the direct comparison runs.
    """
    env_key = normalize_repo_url(env_repo)
    if env_key == normalize_repo_url(declared_repo):
        return True
    if not work_repo_url:
        return False
    derived, _mode = derive_clone_url(declared_repo, work_repo_url)
    return env_key == normalize_repo_url(derived)


def load_sources(path, work_repo_url: Optional[str] = None) -> Tuple[Optional[dict], list]:
    """Load and validate ``sources.yaml`` at ``path``.

    ``work_repo_url`` is the URL the work repo was cloned from; when given,
    the schema-1 trust rule is enforced (declared repos must live on the
    same host — the credential a declared URL receives is the one the
    work-repo URL already carries for that host). ``None`` skips the host
    check, for callers with no work-repo context.

    Returns ``(config, warnings)``:
    - ``(None, [])`` when no file exists — silent legacy mode, zero output.
    - ``(dict, warnings)`` for a valid file: ``{"schema": 1, "sources":
      {"taskdef": {"repo": ..., "ref": ...}, "napkin": {"repo": ...}}}``
      with only declared sources present (``ref`` only when declared);
      ``warnings`` is a list of human-readable strings (unknown keys etc.)
      that never block start.

    Raises SourcesConfigError for anything a present file makes untrustable,
    including PyYAML being unavailable while the file exists.
    """
    path = Path(path)
    if not path.exists():
        # Silent legacy mode: the resolution matrix never engages (spec G5).
        return None, []
    if yaml is None:
        raise SourcesConfigError(
            f"{path} exists but PyYAML is not available to this interpreter; "
            "refusing to start rather than silently ignoring a present "
            "sources.yaml. Install PyYAML (the container image guarantees it) "
            "or remove the file."
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourcesConfigError(
            f"unreadable {path}: {exc}. A present sources.yaml is never "
            "silently ignored — fix its permissions or remove it."
        )
    try:
        data = yaml.safe_load(raw)
    except Exception as exc:
        raise SourcesConfigError(f"unparseable {path}: {exc}")
    if not isinstance(data, dict):
        raise SourcesConfigError(
            f"{path} must be a YAML mapping with `schema:` and `sources:` "
            f"keys, got {type(data).__name__}"
        )

    supported = ", ".join(str(s) for s in SUPPORTED_SOURCES_SCHEMAS)
    schema = data.get("schema")
    # Booleans are explicitly rejected before the int check: `schema: true`
    # is a YAML bool and isinstance(True, int) would let it sail through
    # (same trap read_taskdef_schema in hooks/start.py guards against).
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise SourcesConfigError(
            f"{path} must declare an integer `schema:` (supported: {supported})"
        )
    if schema not in SUPPORTED_SOURCES_SCHEMAS:
        raise SourcesConfigError(
            f"{path} declares schema {schema}, but this loader supports: "
            f"{supported}. Upgrade lmer or rewrite the file for a supported "
            "schema."
        )

    warnings: list = []
    for key in data:
        if key not in ("schema", "sources"):
            warnings.append(
                f"{path}: unknown top-level key `{key}` ignored (forward-compat)"
            )

    sources_map = data.get("sources")
    if sources_map is None:
        sources_map = {}
    if not isinstance(sources_map, dict):
        raise SourcesConfigError(
            f"{path}: `sources:` must be a mapping of source names "
            f"({', '.join(SOURCE_ENV_OVERRIDES)}), got "
            f"{type(sources_map).__name__}"
        )

    config: dict = {"schema": schema, "sources": {}}
    for name, entry in sources_map.items():
        if name not in SOURCE_ENV_OVERRIDES:
            if name in _RESERVED_SOURCE_KEYS:
                warnings.append(
                    f"{path}: `sources.{name}` is reserved for a future "
                    "schema and not supported yet; ignored"
                )
            else:
                warnings.append(
                    f"{path}: unknown key `{name}` under `sources:` ignored "
                    "(forward-compat)"
                )
            continue
        if not isinstance(entry, dict):
            raise SourcesConfigError(
                f"{path}: `sources.{name}` must be a mapping with a `repo:` key"
            )
        repo = entry.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise SourcesConfigError(
                f"{path}: `sources.{name}` must declare a non-empty string `repo:`"
            )
        repo = repo.strip()
        if url_has_embedded_credential(repo):
            # Never echo the credentialed URL — the message itself must be
            # safe to print anywhere.
            raise SourcesConfigError(
                f"{path}: `sources.{name}.repo` embeds a credential "
                f"({scrub_credentials(repo)} carried userinfo with a secret "
                "component). The work repo is shared — never commit tokens. "
                "Strip the credential from the URL; auth is derived from the "
                "work-repo URL at clone time."
            )
        if _url_shape_is_ambiguous(repo):
            # Checked before the trust rule and independently of
            # work_repo_url: an unreadable URL is never trustworthy, and the
            # trust rule's own message ("host '' does not match …") is the
            # wrong place to send an operator who committed a token.
            raise SourcesConfigError(
                f"{path}: `sources.{name}.repo` cannot be read unambiguously — "
                f"{_AMBIGUOUS_SHAPE_REFUSAL}"
            )
        if work_repo_url is not None:
            declared_host = repo_url_host(repo)
            work_host = repo_url_host(work_repo_url)
            if declared_host != work_host:
                raise SourcesConfigError(
                    f"{path}: `sources.{name}.repo` host "
                    f"{declared_host or repr(scrub_credentials(repo))} does "
                    f"not match the work repo host {work_host} (schema-1 "
                    "trust rule: declared sources must live on the same host "
                    "as the work repo). "
                    f"Use the {SOURCE_ENV_OVERRIDES[name]} env var override "
                    "for a cross-host source."
                )
        parsed = {"repo": repo}
        for key in entry:
            if key == "repo":
                continue
            if key == "ref":
                if name != "taskdef":
                    raise SourcesConfigError(
                        f"{path}: `ref:` is valid under `sources.taskdef` "
                        f"only in schema 1 (there is no LMER_NAPKIN_REF); "
                        f"remove `sources.{name}.ref`."
                    )
                ref = entry["ref"]
                if isinstance(ref, bool) or not isinstance(ref, str) or not ref.strip():
                    raise SourcesConfigError(
                        f"{path}: `sources.taskdef.ref` must be a non-empty string"
                    )
                parsed["ref"] = ref.strip()
                continue
            warnings.append(
                f"{path}: unknown key `{key}` under `sources.{name}` ignored "
                "(forward-compat)"
            )
        config["sources"][name] = parsed
    return config, warnings


# --- Doctor CLI (frozen seam, see module docstring) ---------------------------
# One JSON document on stdout, human text on stderr, exit codes
# EXIT_OK / EXIT_REFUSAL / EXIT_USAGE. All URLs pass scrub_credentials
# except `clone_url` behind --emit-clone-urls (the single opt-in).


class _UsageError(Exception):
    """Raised by _ArgumentParser.error so main() owns the usage exit code."""


class _ArgumentParser(argparse.ArgumentParser):
    """argparse variant that reports usage errors as EXIT_USAGE, not 2.

    Stock argparse calls sys.exit(2) on a bad invocation, which would
    collide with EXIT_REFUSAL — the sh caller could not tell "your config
    is bad" from "you called me wrong". Raising lets main() print the
    message to stderr and return EXIT_USAGE with nothing on stdout.
    """

    def error(self, message):
        raise _UsageError(
            f"{self.format_usage().rstrip()}\n{self.prog}: error: {message}"
        )


def _emit(doc, warnings=(), errors=()) -> None:
    """Write the single JSON document to stdout, human lines to stderr."""
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(json.dumps(doc, indent=2))


def _resolve_work_repo_url(args) -> Optional[str]:
    """--work-repo-url with the documented $LMER_REPO_URL fallback."""
    return args.work_repo_url or os.environ.get("LMER_REPO_URL") or None


def _resolve_doctor_work_repo_url(args) -> Optional[str]:
    """Resolve doctor's donor URL, preferring the paired work-repo value."""
    return (
        args.work_repo_url
        or os.environ.get("LMER_WORK_REPO")
        or os.environ.get("LMER_REPO_URL")
        or None
    )


def _work_repo_credential_file() -> Optional[str]:
    """Return lmer's readable session credential-file path, when present."""
    value = os.environ.get("LMER_WORK_REPO_CREDENTIAL_FILE")
    if not value:
        return None
    try:
        return value if Path(value).is_file() else None
    except OSError:
        return None


def _default_doctor_path() -> str:
    """`doctor --path` default: the work-repo clone's sources.yaml.

    Read from the environment at parser-build time (i.e. per invocation, not
    once at import) so the default tracks $LMER_WORK_REPO_PATH the same way
    every other work-repo consumer does — an empty value falls back to /work
    like the env-var fallbacks above.
    """
    root = os.environ.get("LMER_WORK_REPO_PATH") or _DEFAULT_WORK_REPO_PATH
    return str(Path(root) / SOURCES_FILENAME)


def _scrubbed_declared_sources(config) -> dict:
    """The `sources` block of a load_sources config, URLs scrubbed."""
    declared: dict = {}
    for name, entry in config["sources"].items():
        item = {"repo": scrub_credentials(entry["repo"])}
        if "ref" in entry:
            item["ref"] = entry["ref"]
        declared[name] = item
    return declared


def _cmd_normalize(args) -> int:
    """`normalize URL` — canonical comparison form; always exits ok."""
    url = args.url
    # An ambiguous URL still normalizes (this subcommand never refuses), but
    # the verdict below reports only ONE of its two readings — say so on
    # stderr rather than let `had_embedded_credential: false` be read as
    # "no credential here". load_sources/derive refuse the same shape.
    warnings = (
        [f"this URL cannot be read unambiguously — {_AMBIGUOUS_SHAPE_REFUSAL}"]
        if _url_shape_is_ambiguous(url)
        else []
    )
    _emit(
        {
            "ok": True,
            "input": scrub_credentials(url),
            # Scrubbed like every other URL in the document (frozen
            # redaction contract): the normalizer strips userinfo only for
            # the readings the parser recognizes, and this field must not be
            # the one surface that depends on the parser to stay safe.
            "normalized": scrub_credentials(normalize_repo_url(url)),
            "host": repo_url_host(url),
            "had_embedded_credential": url_has_embedded_credential(url),
            "errors": [],
        },
        warnings=warnings,
    )
    return EXIT_OK


def _cmd_derive(args) -> int:
    """`derive DECLARED --work-repo-url URL` — derived clone URL + mode."""
    work_repo_url = _resolve_work_repo_url(args)
    if work_repo_url is None:
        print(
            f"{args.prog}: error: derive needs --work-repo-url "
            "(or $LMER_REPO_URL) to derive auth from",
            file=sys.stderr,
        )
        return EXIT_USAGE
    declared = args.declared_url
    error = None
    if url_has_embedded_credential(declared):
        # Same refusal as load_sources: the doctor surface must never be
        # the thing that launders a committed token into a "derived" URL.
        error = (
            f"declared URL {scrub_credentials(declared)} embeds a "
            "credential; refusing. Strip it — auth is derived from the "
            "work-repo URL."
        )
    elif _url_shape_is_ambiguous(declared):
        # Also mirrors load_sources: deriving from a URL with two readings
        # would carry the work-repo credential onto whichever one this code
        # guessed, and the guess is not knowable.
        error = (
            "declared URL cannot be read unambiguously — "
            f"{_AMBIGUOUS_SHAPE_REFUSAL}"
        )
    if error is not None:
        _emit(
            {
                "ok": False,
                "declared": scrub_credentials(declared),
                "work_repo_url": scrub_credentials(work_repo_url),
                "errors": [error],
            },
            errors=[error],
        )
        return EXIT_REFUSAL
    clone_url, mode = derive_clone_url(declared, work_repo_url)
    _emit(
        {
            "ok": True,
            "declared": scrub_credentials(declared),
            "work_repo_url": scrub_credentials(work_repo_url),
            "mode": mode,
            "anonymous": mode in ANONYMOUS_MODES,
            # The single documented redaction opt-in (module docstring).
            "clone_url": clone_url
            if args.emit_clone_urls
            else scrub_credentials(clone_url),
            "clone_url_redacted": not args.emit_clone_urls,
            "errors": [],
        }
    )
    return EXIT_OK


def _cmd_validate(args) -> int:
    """`validate PATH` — load_sources verdict: sources, warnings, errors."""
    work_repo_url = _resolve_work_repo_url(args)
    try:
        config, warnings = load_sources(args.path, work_repo_url=work_repo_url)
    except SourcesConfigError as exc:
        error = scrub_credentials(str(exc))
        _emit(
            {
                "ok": False,
                "path": args.path,
                # SourcesConfigError is only ever raised for a present file.
                "present": True,
                "schema": None,
                "sources": None,
                "warnings": [],
                "errors": [error],
            },
            errors=[error],
        )
        return EXIT_REFUSAL
    warnings = [scrub_credentials(w) for w in warnings]
    _emit(
        {
            "ok": True,
            "path": args.path,
            "present": config is not None,
            "schema": config["schema"] if config is not None else None,
            "sources": _scrubbed_declared_sources(config)
            if config is not None
            else None,
            "warnings": warnings,
            "errors": [],
        },
        warnings=warnings,
    )
    return EXIT_OK


def _cmd_doctor(args) -> int:
    """`doctor` — aggregated machine-readable check document."""
    work_repo_url = _resolve_doctor_work_repo_url(args)
    work_repo_credential_file = _work_repo_credential_file()
    use_work_repo_credential_file = bool(
        work_repo_credential_file
        and not args.work_repo_url
        and os.environ.get("LMER_WORK_REPO") == work_repo_url
    )
    warnings: list = []
    errors: list = []
    config = None
    present = False
    try:
        config, load_warnings = load_sources(
            args.path, work_repo_url=work_repo_url
        )
        warnings.extend(scrub_credentials(w) for w in load_warnings)
        present = config is not None
    except SourcesConfigError as exc:
        errors.append(scrub_credentials(str(exc)))
        present = True  # only a present file can raise
    declared = None
    if config is not None:
        if work_repo_url is None:
            warnings.append(
                "no work-repo URL (--work-repo-url / $LMER_WORK_REPO / "
                "$LMER_REPO_URL): "
                "trust-rule check and clone-url derivation skipped"
            )
        declared = _scrubbed_declared_sources(config)
        for name, entry in config["sources"].items():
            if work_repo_url is None:
                continue
            clone_url, mode = derive_clone_url(
                entry["repo"],
                work_repo_url,
                work_repo_has_credential_file=use_work_repo_credential_file,
            )
            declared[name]["mode"] = mode
            declared[name]["anonymous"] = mode in ANONYMOUS_MODES
            # The single documented redaction opt-in (module docstring).
            declared[name]["clone_url"] = (
                clone_url if args.emit_clone_urls else scrub_credentials(clone_url)
            )
            declared[name]["clone_url_redacted"] = not args.emit_clone_urls
    _emit(
        {
            "ok": not errors,
            "path": args.path,
            "present": present,
            "schema": config["schema"] if config is not None else None,
            "work_repo_url": scrub_credentials(work_repo_url)
            if work_repo_url
            else None,
            "sources": declared,
            "warnings": warnings,
            "errors": errors,
            "supported_sources_schemas": list(SUPPORTED_SOURCES_SCHEMAS),
            "supported_taskdef_schemas": list(SUPPORTED_TASKDEF_SCHEMAS),
        },
        warnings=warnings,
        errors=errors,
    )
    return EXIT_OK if not errors else EXIT_REFUSAL


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="python3 -m lmer_cli.container.sources",
        description="Canonical source config tooling (doctor-facing CLI; "
        "see the module docstring for the frozen contract).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _common(sub, work_repo_url_help):
        # --json is accepted everywhere as the explicit marker for the
        # always-on behavior: exactly one JSON document on stdout.
        sub.add_argument(
            "--json",
            action="store_true",
            help="emit JSON on stdout (always on; explicit marker flag)",
        )
        sub.add_argument("--work-repo-url", help=work_repo_url_help)

    validate = subparsers.add_parser(
        "validate", help="validate a sources.yaml against load_sources"
    )
    validate.add_argument("path", help="path to the sources.yaml to validate")
    _common(
        validate,
        "work-repo URL for the trust-rule host check "
        "(default: $LMER_REPO_URL; omit both to skip the check)",
    )
    validate.set_defaults(handler=_cmd_validate)

    normalize = subparsers.add_parser(
        "normalize", help="print the normalized comparison form of a URL"
    )
    normalize.add_argument("url", help="git repo URL to normalize")
    normalize.add_argument(
        "--json",
        action="store_true",
        help="emit JSON on stdout (always on; explicit marker flag)",
    )
    normalize.set_defaults(handler=_cmd_normalize)

    derive = subparsers.add_parser(
        "derive", help="derive the clone URL for a declared repo"
    )
    derive.add_argument("declared_url", help="declared source repo URL")
    _common(
        derive,
        "work-repo URL whose auth is borrowed "
        "(default: $LMER_REPO_URL; required one way or the other)",
    )
    derive.add_argument(
        "--emit-clone-urls",
        action="store_true",
        help="emit the derived clone URL unredacted (the single opt-in; "
        "default output is fully redacted)",
    )
    derive.set_defaults(handler=_cmd_derive, prog=derive.prog)

    doctor = subparsers.add_parser(
        "doctor", help="aggregated machine-readable check document"
    )
    doctor.add_argument(
        "--path",
        default=_default_doctor_path(),
        help="sources.yaml location "
        f"(default: $LMER_WORK_REPO_PATH/{SOURCES_FILENAME}, "
        f"currently {_default_doctor_path()})",
    )
    _common(
        doctor,
        "work-repo URL for trust check + clone-url derivation "
        "(default: $LMER_WORK_REPO, then $LMER_REPO_URL; omitted: "
        "derivation is skipped)",
    )
    doctor.add_argument(
        "--emit-clone-urls",
        action="store_true",
        help="emit derived clone URLs unredacted (the single opt-in; "
        "default output is fully redacted)",
    )
    doctor.set_defaults(handler=_cmd_doctor)
    return parser


def main(argv: Optional[list] = None) -> int:
    """Doctor-facing CLI entry point (contract in the module docstring).

    Returns EXIT_OK / EXIT_REFUSAL / EXIT_USAGE; never lets a bad
    invocation write to stdout, so a consumer piping stdout to a JSON
    parser sees either one valid document or nothing.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
