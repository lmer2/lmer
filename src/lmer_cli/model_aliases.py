"""Operator-defined model aliases (issue #309).

``LMER_MODEL_ALIASES="sol=gpt-5.6-sol"`` gives a model id a short local name.
Format, coverage, and what a single pass does and does not promise:
``docs/LMER-CLI.md``, ``LMER_MODEL_ALIASES``.

Two properties are load-bearing:

* **Expansion runs before harness resolution**, so an alias selects the harness
  its real id implies (``--model sol`` runs codex). Expanding later would run the
  right model on the wrong harness, which is the dead session #309 was filed
  about.
* **Unknown is not an error.** lmer has no model catalogue — it never validated a
  model name — so a name that is not an alias passes through verbatim and the
  harness rejects what it does not know.

Loading degrades like :mod:`lmer_cli.presets`: a malformed entry warns and is
skipped, and the parse is cached so one typo is reported once.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .container.dispatch_agents import parse_dispatch_value

#: Env var carrying the alias table, ``alias=model`` pairs separated by commas.
#: Read wherever a model name is resolved; forwarded into the container by the
#: host CLI so in-container consumers see the same table.
MODEL_ALIASES_ENV = "LMER_MODEL_ALIASES"

#: Narrower than a model id on purpose: a tight charset is what makes a malformed
#: entry recognisable instead of silently defining something unusable.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Keyed on the raw value: several readers per launch, one complaint per typo.
_CACHE: Dict[str, Tuple[Dict[str, str], Tuple[str, ...]]] = {}


def parse_model_aliases(raw: Optional[str]) -> Tuple[Dict[str, str], List[str]]:
    """Parse *raw* into an ``{alias: model}`` mapping plus warnings.

    Commas separate, and only the first ``=`` splits — a model id may contain
    anything but a comma, an alias may not contain ``=``. Whitespace is stripped
    (the value becomes a ``--model`` argument), case is not (model ids are
    case-sensitive to whoever resolves them), and a later duplicate wins.
    """
    aliases: Dict[str, str] = {}
    warnings: List[str] = []
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        alias, sep, model = entry.partition("=")
        alias = alias.strip()
        model = model.strip()
        if not sep or not model:
            warnings.append(
                f"{MODEL_ALIASES_ENV}: {entry!r} is not alias=model — skipped"
            )
            continue
        if not _ALIAS_RE.match(alias):
            warnings.append(
                f"{MODEL_ALIASES_ENV}: {alias!r} is not a usable alias name "
                "(letters, digits, dot, dash, underscore) — skipped"
            )
            continue
        aliases[alias] = model
    return aliases, warnings


def load_model_aliases(environ) -> Tuple[Dict[str, str], List[str]]:
    """The alias table from *environ*, with its warnings.

    Warnings come back to the first caller only, which is what keeps one typo
    from being reported once per reader. Keyed on the value rather than cached
    once, because a long-lived host process (the Slack listener) changes the
    environment between launches.
    """
    raw = environ.get(MODEL_ALIASES_ENV) or ""
    if raw not in _CACHE:
        aliases, warnings = parse_model_aliases(raw)
        _CACHE[raw] = (aliases, tuple(warnings))
        return aliases, list(warnings)
    aliases, _warnings = _CACHE[raw]
    return aliases, []


def expand_model_alias(model: Optional[str], aliases: Dict[str, str]) -> Optional[str]:
    """Return *model* with a single alias expansion applied.

    A blank value stays blank: ``--model ''`` means "leave the environment alone"
    everywhere else, and this must not turn it into something.
    """
    if not model:
        return model
    return aliases.get(model.strip(), model)


def expand_dispatch_value(value: Optional[str], aliases: Dict[str, str]) -> Optional[str]:
    """Expand the model half of a ``LMER_DISPATCH_<LANE>`` value.

    Which half is the model is
    :func:`lmer_cli.container.dispatch_agents.parse_dispatch_value`'s decision,
    asked rather than re-derived: it splits on the last colon only for a valid
    effort suffix, so a Bedrock-style ``…-v1:0`` stays one model id instead of
    having its head rewritten through the table.

    The suffix is carried over from the input, not from the parse, which
    lowercases it — expanding an alias is no licence to rewrite the rest.
    """
    if not value:
        return value
    raw = value.strip()
    if not raw:
        return value
    # An alias name cannot contain a colon, so a whole-value hit needs no parse.
    if raw in aliases:
        return aliases[raw]
    parsed = parse_dispatch_value(raw)
    if parsed is None or not parsed.effort:
        return value
    expanded = aliases.get(parsed.model)
    if not expanded:
        return value
    return f"{expanded}:{raw.rpartition(':')[2]}"
