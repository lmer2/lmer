"""Remote-URL keys for the registry — an adapter over the ONE normalizer (D21).

Spec D21: remote normalization has exactly one definition,
``lmer_cli.container.sources.normalize_repo_url``, and the schema ADOPTS its
rules rather than restating them. This module is where the schema side applies
that definition uniformly to the three things §4 compares — a manifest entry's
``repo:``, each of its ``remotes:``, and the URL a session looks up — so all
three meet in the same canonical ``host/path`` form and a multi-remote project
lands in ONE tree whichever spelling the session arrived with (D8).

It is deliberately tiny and contains NO URL parsing of its own: no scheme or
userinfo regex, no ``urllib``, no string dissection on ``://``/``@``/``:``.
Credential stripping, the default-port rule (drop :443/:22, keep any other
port as ``host:port`` — a different port can be a different repo) and the
scp/ssh folding all live in ``sources.py`` and its tests. A private
reimplementation here would be a security regression, not a refactor: the next
credential-boundary fix there would silently not apply here.
``tests/test_lmer_schema_urls.py`` guards this on the module source.

Three functions, two schema contracts:

- ``normalize(url)`` is the canonical SPELLING — what the tool writer emits into
  ``_lmer.yaml`` and what a "non-canonical spelling" WARN compares against.
  It is ``normalize_repo_url`` verbatim: host lowered, path case preserved as
  written.
- ``lookup_key(url, schema=1)`` is the COMPARISON key. Schema 1 folds the
  spelling to lower case for compatibility. Schema 2 retains path case as
  repository identity; the shared normalizer already lowercases the host.
  ``entry_keys`` applies the same versioned key to an entry's full union.

Re-export decision — ``repo_url_host`` and ``scrub_credentials`` are NOT
re-exported; callers that need them import from ``lmer_cli.container.sources``
directly. ``repo_url_host`` answers a different question with a different key
(the sources same-host trust rule: host only, port-blind), and re-exporting it
here would suggest it is a registry key when it is not. ``scrub_credentials``
is output redaction, not identity; it belongs next to whatever prints a URL
(validator findings, doctor), not in the module that computes keys — and a
second import spelling for either is drift bait for no gain. Keeping the only
``lmer_cli`` import here ``normalize_repo_url`` also makes the drift guard
simple to state.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple

from lmer_cli.container.sources import normalize_repo_url

__all__ = ["normalize", "lookup_key", "entry_keys"]


def normalize(url) -> str:
    """Canonical spelling of a remote: ``host/path`` per ``normalize_repo_url``.

    Pure delegation (D21). Empty string for an empty/``None`` value, exactly
    as the normalizer returns it; validating that a ``repo:`` is present and
    well-formed is the registry's job, not this module's.
    """
    return normalize_repo_url(url)


def lookup_key(url, schema: int = 1) -> str:
    """Comparison key of a remote under the requested root schema.

    Schema 1 folds the normalized spelling to preserve its historical
    comparison. Schema 2 keeps path case. Both retain non-default ports.
    """
    spelling = normalize(url)
    return spelling if schema >= 2 else spelling.lower()


def entry_keys(
    repo: Optional[str],
    remotes: Optional[Iterable[str]] = None,
    schema: int = 1,
) -> Tuple[str, ...]:
    """The lookup keys one registry entry answers to: ``repo:`` first, then
    its ``remotes:`` in declared order, each normalized, without duplicates.

    *schema* selects the same comparison contract as :func:`lookup_key`.
    Empty values contribute nothing, so a repoless project (D19) is ``()`` and
    a single-remote project is a 1-tuple. Only normalizes: two values that
    fold to the same key are collapsed here, and REJECTING that (``repo:``
    repeated in ``remotes:``, two remotes spelling one repo — §4 registry
    corners) is the registry validator's job, which can see it in the count
    shrinking or by keying each value itself. *remotes* is the manifest's
    list, already shape-checked by the caller.
    """
    keys = []
    for value in (repo, *(remotes or ())):
        key = lookup_key(value, schema=schema)
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)
