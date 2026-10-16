"""The validator-backed half of the control UI's schema views (#374, spec §13.F).

Three things the UI shows come from ONE in-process ``lmer-schema validate
--json`` run over the daemon's mirror: the tree-form flag, the validator's
findings document, and the host buckets awaiting adoption. They live in this
module rather than in :mod:`lmer_platform.workrepo` because that module's
header keeps the check registry off the fleet read path — ``workrepo`` sorts
directory names, it does not import a validator to do it — and nothing that
polls with the fleet calls in here.

The document is the CLI's, never re-spelled
-------------------------------------------
:func:`report` returns ``lmer_schema.cli.json_document(lmer_schema.checks.
runner.validate(mirror, ...))`` with every option resolved by the CLI's own
parser (:func:`lmer_schema.cli.build_parser`) and its own ``--plexus-repo-url``
fallback, so the default stale window, the envelope and every finding are
exactly what ``lmer-schema validate <mirror> --json`` prints for the same flags
— the test compares the two field for field. ``tier`` is computed BESIDE that
document from its form and never written into it.

DECIDE — the ``--plexus-repo-url`` guard is armed
-------------------------------------------------
From ``config.work_repo_url``, credential-scrubbed. The mirror IS the work
repo, cloned from that URL, and "PATH is the work repo itself" is precisely
the case the CLI arms the manifest's ``sources:`` same-host guard for
(``lmer_schema.cli._plexus_repo_url``: ``$LMER_WORK_REPO`` when PATH is
``$LMER_WORK_REPO_PATH``). Leaving it dormant would show the operator a cleaner
verdict than the same command prints inside any container for the same tree.
Scrubbed (:func:`lmer_platform.workrepo._clean_url`) because the configured URL
is routinely tokenised and it is both a cache key here and a value the guard
may quote back into a finding; the host the guard compares survives the scrub.
No URL configured passes no flag, and then the CLI's own fallback decides.

DECIDE — the cache: keyed by revision, expiring after 10 minutes
-----------------------------------------------------------------
A validation walks the whole tree, so a result is cached under a lock the way
:data:`lmer_platform.workrepo._RESLUGGED_INDEX` is, keyed by (mirror, HEAD sha,
every validator option including the scrubbed work-repo URL). The HEAD is the
one the mirror's last pull recorded (:func:`lmer_platform.workrepo.
mirror_status`) — the mirror only changes through that pull, and reading
``mirror.json`` costs no subprocess. With no recorded HEAD nothing is cached:
an unknown revision cannot say whether the tree changed.

A revision alone is NOT a complete key, and that is why entries also expire
(astra PR7): ``record.session-stale`` and the "N days ago" text are a function
of the clock, so the same HEAD and the same record bytes can owe a different
document an hour later. :data:`CACHE_SECONDS` is 10 minutes — longer than the
UI's poll so a panel left open costs one walk per interval rather than one per
poll, and short enough that a record crossing the 14-day window shows up
within one coffee rather than at the next push. ``?refresh=1`` on the route
bypasses it (and stores the fresh result).

What this never does
--------------------
It never pulls (the fleet poll owns the mirror's freshness) and never
validates a legacy tree: a mirror with no manifest answers ``form: "legacy"``
with no document, and an absent mirror answers ``form: None``. A broken
manifest is not an exception — the validator marks the report unusable
(exit code 2) and the route answers it with a 200.

Host buckets come from the document
-----------------------------------
:func:`buckets` reads the document's own ``tree.host-bucket`` findings — no
second walk, and no listing beside ``work housekeep`` (§13.C). Each carries the
copyable line ``work adopt <host>/<project> <short-name>``; the short name is
a placeholder the operator replaces and is NEVER derived (D9: names are never
derived from a path). There is no adopt route: the panel is a read view
(§13.C, astra §4 cut 6).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lmer_schema import has_manifest

# The CLI is the one owner of "what does validate print": its parser for the
# defaults, its URL fallback, its stale-window unit and its envelope. Private
# helpers imported deliberately, the trade ``workrepo`` makes with
# ``_scrub_credentials`` — a second spelling of any of them is a document that
# drifts from the command's.
from lmer_schema.cli import (
    _SECONDS_PER_DAY,
    _plexus_repo_url,
    build_parser,
    json_document,
)
from lmer_schema.checks.runner import validate

from .config import PlatformConfig
from .workrepo import _clean_url, mirror_status

logger = logging.getLogger("lmer_platform.schema_report")

__all__ = [
    "ADOPT_PLACEHOLDER", "BUCKET_CODE", "CACHE_SECONDS", "SCHEMA_NOTE",
    "buckets", "clear_cache", "mirror_form", "report", "workrepo_schema",
]

#: How long a validation stays served at an unchanged HEAD (module docstring).
CACHE_SECONDS = 10 * 60
#: The validator code a host bucket is reported under.
BUCKET_CODE = "tree.host-bucket"
#: The short name in the adopt line: a placeholder, never derived (D9).
ADOPT_PLACEHOLDER = "<short-name>"
SCHEMA_NOTE = (
    "lmer-schema validate --json over this daemon's mirror, exactly as the CLI "
    "prints it. Cached per mirror revision for up to ten minutes, because "
    "staleness findings move with the clock; ?refresh=1 re-validates now. No "
    "pull is forced. A host bucket is adopted with its `work adopt` line, after "
    "choosing the short name."
)

_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _clock() -> float:
    """Wall-clock seconds, isolated so tests can freeze it. Both the validator's
    *now* and the cache's expiry read it, so a frozen clock freezes both."""
    return time.time()


def clear_cache() -> None:
    """Drop every cached validation (tests, and anything that knows better)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def mirror_form(config: PlatformConfig) -> Optional[str]:
    """``"schema"``, ``"legacy"`` or ``None`` for an absent mirror — no validation.

    A presence test (:func:`lmer_schema.has_manifest`, D20's one bit), cheap
    enough for the fleet payload: nothing is opened, so a broken manifest is
    still schema form, exactly as the validator sees it.
    """
    root = config.mirror_path
    if has_manifest(root):
        return "schema"
    return "legacy" if root.is_dir() else None


def _options(config: PlatformConfig, mirror: Path) -> tuple:
    """``(argv, args)``: the CLI invocation this report stands for, parsed by
    the CLI's own parser so every default is the command's."""
    argv = ["validate", str(mirror), "--json"]
    url = _clean_url(config.work_repo_url or "")
    if url:
        argv += ["--plexus-repo-url", url]
    return argv, build_parser().parse_args(argv)


def report(config: PlatformConfig, *, refresh: bool = False) -> Optional[dict]:
    """The validate document for the mirror, or ``None`` when it is not schema form.

    See the module docstring for the cache and the guard decision.
    """
    if mirror_form(config) != "schema":
        return None
    mirror = config.mirror_path.resolve()
    _argv, args = _options(config, mirror)
    plexus_repo_url = _plexus_repo_url(args)
    stale_after = args.stale_after * _SECONDS_PER_DAY
    head = mirror_status(config).head_sha
    key = (str(mirror), head, plexus_repo_url, stale_after)
    now = _clock()
    with _CACHE_LOCK:
        if head is not None and not refresh:
            cached = _CACHE.get(key)
            if cached is not None and now - cached[0] < CACHE_SECONDS:
                return cached[1]
        document = json_document(validate(
            args.path,
            plexus_repo_url=plexus_repo_url,
            now=datetime.fromtimestamp(now, timezone.utc),
            stale_after=stale_after,
        ))
        if head is not None:
            # One entry per mirror: an older revision's document is never
            # served again, so it is not kept.
            for stale_key in [k for k in _CACHE if k[0] == key[0]]:
                del _CACHE[stale_key]
            _CACHE[key] = (now, document)
    return document


def buckets(document: Optional[dict]) -> list:
    """The host buckets the document reports, each with its adopt line."""
    if not document:
        return []
    rows = []
    for finding in document.get("findings") or ():
        if finding.get("code") != BUCKET_CODE:
            continue
        path = finding.get("path")
        rows.append({
            "path": path,
            "message": finding.get("message"),
            "adopt_command": f"work adopt {path} {ADOPT_PLACEHOLDER}",
        })
    return rows


def _tier(form: Optional[str]) -> Optional[str]:
    """Which severities the findings are at: the schema form's own, or the
    legacy tier's downgrades (``docs/SCHEMA.md``, "Strictness tiers"). A host
    bucket's contents are at the legacy tier even in schema form, and the
    document's per-finding severities already say so; this is the tree's."""
    if form is None:
        return None
    return "legacy" if form == "legacy" else "schema"


def workrepo_schema(config: PlatformConfig, *, refresh: bool = False) -> dict:
    """``{form, tier, document, buckets, note}`` — the ``GET /api/workrepo/schema`` reply."""
    form = mirror_form(config)
    document = report(config, refresh=refresh) if form == "schema" else None
    return {
        "form": form,
        "tier": _tier(document.get("form") if document else form),
        "document": document,
        "buckets": buckets(document),
        "note": SCHEMA_NOTE,
    }
