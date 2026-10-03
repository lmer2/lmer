"""Threads a run is pointed at, without being connected to them (issue #414).

A run is often *about* a discussion that happened somewhere else, or owes one
result to a thread it does not own. Connecting it would post an ack, announce the
backlog, keep the run listening and take the thread from anyone else who wants
it. A **reference** does none of that: it is a permalink and a title, attached to
a run, that the run is told about once — after which `lmer-signal read` and
`lmer-signal post --thread` are how it uses them.

Platform state, for :mod:`lmer_platform.meta`'s reasons and with its
consequences: the daemon never writes run state (spec D3), its work-repo mirror
is force-reset on every pull, so this lives here — local to this orchestrator,
invisible to anyone else's fleet view, and dropped when the run is forgotten.

Its own snapshot rather than a field on the tracked-run index, again for
:mod:`lmer_platform.meta`'s reasons: a lost update there loses a *run*, and a
write through ``track()`` would refresh ``last_seen`` and shove a finished run to
the top of the fleet.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from lmer_cli.threads import ThreadError, parse_thread

from . import meta
from .runs import get_tracked, run_key
from .store import StoreError, mutating, read_json, snapshot_path, utc_now_iso, write_json

logger = logging.getLogger("lmer_platform.references")

__all__ = [
    "REFERENCES_FILE", "MAX_TITLE_CHARS", "MAX_PER_RUN", "LOCALITY_NOTE",
    "ThreadReferenceError", "ReferenceRunNotTracked", "ThreadReference",
    "load_all", "listing", "add", "remove", "drop", "pending", "mark_announced",
    "announcement",
]

REFERENCES_FILE = "run_references.json"

#: One line beside a permalink in a menu. The same bound and the same cleaner as
#: a run's own title (:mod:`lmer_platform.meta`): two cleaners meant to agree and
#: separately maintained are two cleaners that will stop agreeing.
MAX_TITLE_CHARS = meta.MAX_TITLE_CHARS

#: References one run may carry. The same bound a launch list takes: past a
#: dozen, a list of context threads is not context any more.
MAX_PER_RUN = 16

LOCALITY_NOTE = (
    "a reference is this orchestrator's note that a run should know about a "
    "thread. It connects nothing — the run reads and posts with `lmer-signal "
    "read` / `post --thread` — and it lives in platform state, so it is "
    "invisible to anyone else's fleet view and goes when the run is forgotten."
)


class ThreadReferenceError(RuntimeError):
    """A refusal, carrying the status a route should answer with.

    Same convention as :mod:`lmer_platform.meta`: the status rides on the
    exception so the routes get one handler and a refusal added later arrives
    with its own code instead of falling through to a 500.

    Named away from the builtin ``ReferenceError``: an ``except ReferenceError``
    in this module — or in anything importing from it — would catch something
    other than what a reader expects, and the shadowing is invisible at the
    catch site.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class ReferenceRunNotTracked(ThreadReferenceError):
    """A reference on a run this orchestrator does not track: 404 is honest —
    there is no run in this fleet to attach a thread to.

    Distinct from :class:`lmer_platform.meta.RunNotTracked`, and named so: the
    two are unrelated classes and ``api.py`` handles both modules in adjacent
    routes, where one name for two things is an ``except`` that silently misses.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status=404)


@dataclass(frozen=True)
class ThreadReference:
    id: str
    permalink: str
    title: str
    channel: str
    added_at: str
    added_by: str = ""
    announced_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "permalink": self.permalink,
            "title": self.title,
            "channel": self.channel,
            "added_at": self.added_at,
            "added_by": self.added_by,
            "announced_at": self.announced_at,
            # A fact from the daemon rather than a rule the UI restates.
            "announced": bool(self.announced_at),
        }

    @classmethod
    def from_dict(cls, payload: object) -> Optional["ThreadReference"]:
        """Rebuild one, tolerantly: a hand-edited entry must not fail the read of
        a whole run's references."""
        if not isinstance(payload, dict):
            return None
        permalink = payload.get("permalink")
        if not isinstance(permalink, str) or not permalink:
            return None
        return cls(
            id=str(payload.get("id") or _reference_id(permalink)),
            permalink=permalink,
            title=str(payload.get("title") or ""),
            channel=str(payload.get("channel") or ""),
            added_at=str(payload.get("added_at") or ""),
            added_by=str(payload.get("added_by") or ""),
            announced_at=payload.get("announced_at") if isinstance(payload.get("announced_at"), str) else None,
        )


def _reference_id(permalink: str) -> str:
    """Derived from the link, so adding the same **link** twice is one reference
    and a ``DELETE`` has something stable to address.

    The link and not the thread: :func:`add` asks neither Slack nor the
    homeserver anything, so a room named by its alias and the same room named by
    its id are two references — and the run is told about both. That is the price
    of a note nobody's directory can refuse to write; a connection, where two
    records mean two connectors and every reply twice, resolves instead
    (:meth:`lmer_platform.connectors.ThreadConnectors._resolved`).
    """
    return hashlib.sha256(permalink.encode("utf-8")).hexdigest()[:16]


def _path():
    return snapshot_path(REFERENCES_FILE)


def load_all() -> dict:
    try:
        stored = read_json(_path())
    except StoreError as exc:
        logger.error("platform_run_references_unreadable error=%s — treating as empty", exc)
        return {}
    if not stored:
        return {}
    entries = stored.get("references")
    return entries if isinstance(entries, dict) else {}


def _save(entries: dict) -> None:
    write_json(_path(), {"references": entries})


def listing(host: str, project: str, slug: str) -> list:
    """This run's references, oldest first. Empty when it has none."""
    key = run_key(host, project, slug)
    return [
        reference.to_dict()
        for reference in _records(load_all().get(key))
    ]


def _records(raw: object) -> list:
    if not isinstance(raw, list):
        return []
    found = [ThreadReference.from_dict(item) for item in raw]
    return [item for item in found if item is not None]


def add(
    host: str, project: str, slug: str, permalink: object, *,
    title: object = None, by: str = "",
) -> ThreadReference:
    """Attach a thread to a run. Refuses a run this orchestrator does not track.

    The link is parsed but nothing is asked of Slack or matrix: a reference is a
    note, and a directory that is down must not be what stops an operator writing
    one. Whether the token can actually see the thread is answered the first time
    the run reads it.
    """
    ref = parse_thread(permalink)
    clean_title = _cleaned_title(title) or ref.label
    key = run_key(host, project, slug)
    if get_tracked(host, project, slug) is None:
        raise ReferenceRunNotTracked(
            f"{key} is not tracked by this orchestrator, so there is nothing to "
            "attach a thread to. Adopt it first (POST /api/runs/adopt), or spawn it."
        )
    with mutating(_path()):
        entries = load_all()
        current = _records(entries.get(key))
        reference = ThreadReference(
            id=_reference_id(ref.canonical),
            permalink=ref.canonical,
            title=clean_title,
            channel=ref.channel,
            added_at=utc_now_iso(),
            added_by=by,
        )
        kept = [item for item in current if item.id != reference.id]
        if len(kept) >= MAX_PER_RUN:
            raise ThreadReferenceError(
                f"{key} already references {len(kept)} threads; the limit is {MAX_PER_RUN}"
            )
        entries[key] = [item.to_dict() for item in kept] + [reference.to_dict()]
        _save(entries)
    return reference


def remove(host: str, project: str, slug: str, reference_id: str) -> ThreadReference:
    key = run_key(host, project, slug)
    with mutating(_path()):
        entries = load_all()
        current = _records(entries.get(key))
        gone = next((item for item in current if item.id == reference_id), None)
        if gone is None:
            raise ThreadReferenceError(
                f"{key} references no thread {reference_id!r}", status=404
            )
        kept = [item.to_dict() for item in current if item.id != reference_id]
        if kept:
            entries[key] = kept
        else:
            entries.pop(key, None)
        _save(entries)
    return gone


def drop(host: str, project: str, slug: str) -> int:
    """Forget a run's references. Called when the run itself is forgotten."""
    key = run_key(host, project, slug)
    with mutating(_path()):
        entries = load_all()
        gone = _records(entries.pop(key, None))
        if gone:
            _save(entries)
    return len(gone)


def pending(host: str, project: str, slug: str) -> list:
    """References this run has not been told about yet."""
    key = run_key(host, project, slug)
    return [item for item in _records(load_all().get(key)) if not item.announced_at]


def mark_announced(host: str, project: str, slug: str, reference_id: str) -> None:
    """Record that the run was told. Once, so a restart does not tell it again."""
    key = run_key(host, project, slug)
    with mutating(_path()):
        entries = load_all()
        current = _records(entries.get(key))
        if not current:
            return
        entries[key] = [
            {**item.to_dict(), "announced_at": utc_now_iso()}
            if item.id == reference_id and not item.announced_at
            else item.to_dict()
            for item in current
        ]
        _save(entries)


def announcement(reference: ThreadReference) -> str:
    """What the run is told, once. Names the two verbs, because the reference is
    of no use to a session that does not know they exist."""
    return (
        f'reference thread "{reference.title}": {reference.permalink} — '
        "lmer-signal read / post --thread"
    )


def checked_title(value: object) -> str:
    """The title as it would be stored, or :class:`ThreadReferenceError`.

    Public because a spawn validates one before it starts a container: a title
    refused only at the point of storage is refused after the session is up,
    where the only thing left to do with it is log it.
    """
    return _cleaned_title(value)


def _cleaned_title(value: object) -> str:
    if value is None:
        return ""
    try:
        return meta._cleaned(
            value, field="title", limit=MAX_TITLE_CHARS, one_line=True
        ) or ""
    except meta.MetaError as exc:
        raise ThreadReferenceError(str(exc)) from None
