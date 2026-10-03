"""The `host/project/slug` grammar, in one place for every Python consumer.

A run's identity is spelled two ways — a key in the platform's state files
(`gitlab.example.com/acme/widget/develop-141`) and an `lmer://run/…` reference
an agent can write into chat (#241) — and each had grown a copy per consumer
(#383 item 3). `web/src/runref.js` is the browser half; the two are held
together by `tests/test_runref_grammar.py`, which runs one corpus through both.

It lives in `lmer_cli` and imports nothing from `lmer_platform` or `slack_chat`
so both of those can depend on it.

Two grammars, deliberately: :func:`split` is permissive because `ctl` only
shapes an argument and a key naming no run must reach the daemon for a refusal
that names the field; :func:`parse_reference` is anchored because it decides
whether something in somebody's markdown becomes a live link. See
docs/PLATFORM-QUICKSTART.md, "Run references in chat".
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

__all__ = [
    "SEGMENT", "REFERENCE_PREFIX", "REFERENCE_RE", "MARKDOWN_REFERENCE_RE", "JS_TRIM",
    "compose", "split", "parse_reference", "strip_references",
]

#: One path segment of a key: alphanumeric first, then the punctuation a host
#: name, a nested project path element and a derived slug can contain.
#: `run_state.derive_slug` sanitizes a slug down to this set.
SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"

#: The scheme half of a reference. Not a general URL: there is no verb and no
#: query, so a reference can only ever select a view.
REFERENCE_PREFIX = "lmer://run/"

#: The characters JavaScript's `String.prototype.trim` removes: ECMA-262's
#: WhiteSpace plus its LineTerminator set. Spelled out rather than left to
#: `str.strip()`, which trims by `str.isspace()` — a different set, and six
#: padded hrefs parsed differently in the two languages because of it.
JS_TRIM = (
    "\t\n\v\f\r \u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)

#: A whole reference and nothing else. Anchored at both ends, so anything
#: carrying a query or a traversal is simply not one. Three or more segments,
#: because a project path contributes several (`acme/widget`).
REFERENCE_RE = re.compile(
    rf"^{re.escape(REFERENCE_PREFIX)}{SEGMENT}(?:/{SEGMENT}){{2,}}$"
)

#: A reference as it appears in markdown, with the label it shows.
#: `(?<!\\)` because `\[x](lmer://…)` is literal text in CommonMark, not a link.
MARKDOWN_REFERENCE_RE = re.compile(
    r"!?(?<!\\)\[([^\]\n]*)\]\("
    rf"{re.escape(REFERENCE_PREFIX)}({SEGMENT}(?:/{SEGMENT}){{2,}})"
    r"\)"
)


def compose(host: str, project: str, slug: str) -> str:
    """The three parts as the key every run-keyed route and state file uses."""
    return f"{host}/{project}/{slug}"


def split(key: str) -> Optional[Tuple[str, str, str]]:
    """`(host, project, slug)` from a key, or ``None`` if it is not one.

    A project is `group/subgroup`, so the middle is whatever lies between the
    first segment and the last. Shape only: the parts are not checked for
    emptiness or against :data:`SEGMENT`, so a key naming no run comes back as
    three fields and is refused by whatever looks the run up.
    """
    if not isinstance(key, str):
        return None
    parts = key.split("/")
    if len(parts) < 3:
        return None
    return parts[0], "/".join(parts[1:-1]), parts[-1]


def parse_reference(href: str) -> Optional[dict]:
    """`{host, project, slug, key}` for a reference, or ``None`` for anything else.

    Total: not-a-reference is ``None`` rather than an error, which is how every
    caller reads it. The JavaScript twin is `parseRunRef` in `web/src/runref.js`.
    """
    if not isinstance(href, str):
        return None
    raw = href.strip(JS_TRIM)
    if not REFERENCE_RE.match(raw):
        return None
    parts = split(raw[len(REFERENCE_PREFIX):])
    if parts is None:  # pragma: no cover - the regex already required three
        return None
    host, project, slug = parts
    return {
        "host": host,
        "project": project,
        "slug": slug,
        "key": compose(host, project, slug),
    }


def strip_references(text: str) -> str:
    r"""Reduce every markdown run reference in *text* to the label it shows.

    An escaped `\[` is not a link and is left whole; an empty label keeps the
    key so nothing is dropped. **The substitution is context-free** — it does
    not know what a code span or a fenced block is, so a reference written as an
    *example* is reduced too.
    """
    return MARKDOWN_REFERENCE_RE.sub(lambda m: m.group(1) or m.group(2), text)
