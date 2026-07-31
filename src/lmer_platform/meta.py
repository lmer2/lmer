"""What a run is *about*, in this orchestrator's own words (T52).

Two fields — a one-line ``title`` and a markdown ``description`` — attached to a
run this orchestrator tracks, so a fleet row can be identified at a glance
instead of being read off a slug and a taskdef. The orchestrator agent sets them
when it starts a run; the operator can set or correct them at any time.

Why this is platform state and not run state
--------------------------------------------
Because the platform never writes run state (spec D3). Its copy of the work repo
is a **read-only mirror the daemon force-resets on every pull**
(``git reset --hard FETCH_HEAD`` in :mod:`lmer_platform.workrepo`), so a title
written beside ``goals.md`` / ``ledger.yaml`` / ``state.yaml`` would survive
exactly until the next fetch and then vanish with no error anywhere — the worst
available failure mode for a field whose whole job is to still be there
tomorrow. Writing it *through* the container instead (the only sanctioned way
run state changes) would mean spawning a session to rename a run, which is
absurd for a label.

So it lives here, and that has a consequence worth stating outright rather than
discovering:

    **This metadata is local to this orchestrator.** It is not part of the run.
    It does not travel with the run to another operator's fleet view, it is not
    visible to the agent inside the container, and a second orchestrator of your
    own — a laptop as well as a server — has its own, unrelated copy. It is
    removed when the run is forgotten (:func:`lmer_platform.runs.forget`),
    because it describes a run in *this* fleet and nothing else could ever reach
    it afterwards.

That is a real limitation of the D3-compliant design and not an oversight. The
alternative that syncs is the one that gets silently destroyed.

Why its own snapshot rather than two fields on the tracked-run index
--------------------------------------------------------------------
:mod:`lmer_platform.runs` is the closest existing thing — same key, same
lifetime, one more field would have been three lines — and it was rejected for
two specific reasons:

- **A lost update there loses a run, not a title.** ``store`` gives each write
  atomicity but explicitly not consistency across a read-modify-write, and the
  API's handlers are synchronous, which means Starlette runs them in a
  threadpool and two of them really are concurrent. A metadata write racing a
  spawn's ``track()`` over one file drops whichever landed first: today that
  costs a tracked run, i.e. a row disappearing from the fleet. Kept in a
  separate file, the worst a racing rename can do is lose a rename.
- **The cheap version of it is wrong in a way nobody would notice.** Editing a
  title through ``track()`` refreshes ``last_seen``, and ``last_seen`` is what
  ``list_tracked`` orders by — so renaming a finished run would shove it to the
  top of the fleet as if it had just done something.

What is rejected
----------------
Deriving a title from the run's goal. The goal is the *instruction* the run was
given: frequently a paragraph, frequently stale by the time anyone is scanning
the fleet for this run, and never a label. The operator asked for something the
user can identify a run by, which is a different thing from the first sixty
characters of what it was told to do.

Trust
-----
Both fields are writable over the API by an agent holding the shared secret, so
the text is untrusted on the way in and on the way out. In: bounded length,
control characters removed, and the title collapsed to a single line. Out: the
browser renders the description through the one component in the UI that is
allowed to turn text into markup (``web/src/components/Markdown.vue``), never a
second render path.

Writes are loud and reads are quiet, the same split
:mod:`lmer_platform.store` makes: a refused write must reach whoever typed the
text, while an unreadable snapshot must not take a fleet view down with it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from .runs import get_tracked, run_key
from .store import (
    StoreError, mutating, read_json, snapshot_path, utc_now_iso, write_json,
)

logger = logging.getLogger("lmer_platform.meta")

__all__ = [
    "META_FILE", "MAX_TITLE_CHARS", "MAX_DESCRIPTION_CHARS", "LOCALITY_NOTE",
    "MetaError", "RunNotTracked", "RunMeta",
    "load_all", "read", "write", "drop",
]

META_FILE = "run_meta.json"

#: One line, and short enough to sit in a fleet row beside a state chip on a
#: phone without wrapping to three lines. A label that has to be truncated to be
#: shown is not doing the job the operator asked for.
MAX_TITLE_CHARS = 120

#: Ceiling on the description. Matches :data:`lmer_platform.lifecycle.MAX_NOTE_CHARS`
#: and is here for the same reason: the text is typed into a browser (or written
#: by an agent, which is worse) and ends up in a file on disk, so it is bounded
#: rather than trusted to be reasonable. Two thousand characters is several
#: paragraphs — enough to say what a run is for and why, which is the use.
MAX_DESCRIPTION_CHARS = 2000

#: Said in every reply that carries metadata, because the surprising half of this
#: feature is what it is *not*: a property of the run. Prose rather than a flag
#: alone, since the flag only means something to a reader who already knows.
LOCALITY_NOTE = (
    "this title and description are this orchestrator's own note about the run. "
    "They live in platform state, never in the work repo (spec D3, whose mirror "
    "is force-reset on every pull), so they are invisible to anyone else's fleet "
    "view and to the agent in the container, and they are removed when the run "
    "is forgotten."
)

#: C0 controls and DEL, minus the two that mean something in a description: a
#: newline and a tab. Stripped rather than escaped because there is no reading of
#: this text in which a bell or a NUL was intended — and this is agent-written
#: content that lands in a JSON file an operator opens in an editor, in a
#: plain-text API reply, and in a browser.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MetaError(RuntimeError):
    """A refusal, carrying the status a route should answer.

    Same convention as :mod:`lmer_platform.lifecycle` and
    :mod:`lmer_platform.session_io`: the status rides on the exception so the
    routes get one handler and a refusal added later arrives with its own code
    instead of falling through to a 500 with a traceback.
    """

    status = 400


class RunNotTracked(MetaError):
    """Metadata was set on a run this orchestrator does not track.

    Its own class because it is the one refusal here that is about *state*
    rather than about the text, and because 404 is the honest answer: there is
    no run in this fleet to attach a note to.
    """

    status = 404


@dataclass(frozen=True)
class RunMeta:
    """One run's title and description. Absent metadata is an empty one, not None.

    An empty instance rather than ``None`` so every caller renders the same shape
    — a run with no metadata is the overwhelmingly common case, and making it the
    absent-value case would put a branch in every consumer.
    """

    title: str = ""
    description: str = ""
    updated_at: Optional[str] = None

    @property
    def empty(self) -> bool:
        return not (self.title or self.description)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "updated_at": self.updated_at,
            # A fact from the daemon rather than a rule the UI restates: "nothing
            # here yet" is a state both ends have to agree on.
            "empty": self.empty,
        }

    @classmethod
    def from_dict(cls, key: str, payload: Optional[dict]) -> "RunMeta":
        """Rebuild an entry, tolerantly. Anything unusable reads as empty.

        One hand-edited or version-skewed entry must not be able to fail a read
        of the whole file — the same discipline
        :meth:`lmer_platform.runs.TrackedRun.from_dict` applies, for the same
        reason.
        """
        if not isinstance(payload, dict):
            if payload is not None:
                logger.warning("platform_run_meta_malformed key=%s", key)
            return cls()
        title = payload.get("title")
        description = payload.get("description")
        updated_at = payload.get("updated_at")
        return cls(
            title=title if isinstance(title, str) else "",
            description=description if isinstance(description, str) else "",
            updated_at=updated_at if isinstance(updated_at, str) else None,
        )


def _meta_path():
    return snapshot_path(META_FILE)


def load_all() -> dict:
    """Raw ``{key: entry}`` mapping. Empty when nothing has been described yet."""
    try:
        stored = read_json(_meta_path())
    except StoreError as exc:
        # A corrupt file must not take the run's whole detail view with it; the
        # bad bytes have already been moved aside for post-mortem.
        logger.error("platform_run_meta_unreadable error=%s — treating as empty", exc)
        return {}
    if not stored:
        return {}
    entries = stored.get("meta")
    return entries if isinstance(entries, dict) else {}


def _save(entries: dict) -> None:
    write_json(_meta_path(), {"meta": entries})


def _cleaned(
    value: Optional[str], *, field: str, limit: int, one_line: bool
) -> Optional[str]:
    """Validate and normalise one field. ``None`` in means ``None`` out.

    Length is checked *after* normalising, the same order
    :func:`lmer_platform.lifecycle._validated_note` uses: text that is only long
    because it arrived with CRLFs or trailing padding should not be refused for
    it, and the number in the refusal should be the number that was stored.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise MetaError(f"{field} must be text, got {type(value).__name__}")

    # Line endings first, so a CRLF paste is not counted twice and a lone CR
    # cannot survive as an invisible character that overwrites a rendered line.
    normalised = _CONTROL_CHARS.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    if one_line:
        normalised = " ".join(normalised.split())
    else:
        normalised = "\n".join(
            line.rstrip() for line in normalised.split("\n")
        ).strip()

    if len(normalised) > limit:
        raise MetaError(
            f"{field} is {len(normalised)} characters; the limit is {limit}"
        )
    return normalised


def read(host: str, project: str, slug: str) -> RunMeta:
    """This orchestrator's note about a run. Empty when there is none.

    Quiet about scope on purpose, unlike :func:`write`: reading metadata for a
    run that is not tracked answers "there is none" rather than 404, because the
    caller asked a question about a run and the true answer is empty. The
    tracked check exists to stop unreachable state being *created*; refusing a
    read would only turn a run forgotten in another tab into an error page.
    """
    key = run_key(host, project, slug)
    return RunMeta.from_dict(key, load_all().get(key))


def write(
    host: str,
    project: str,
    slug: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> RunMeta:
    """Set a run's title, description, or both. Returns what was stored.

    ``None`` leaves a field alone and ``""`` clears it, so a caller that knows
    about one field cannot blank the other by omission — the orchestrator agent
    setting a title must not silently delete a description an operator wrote.

    Refuses a run this orchestrator does not track. Metadata is a note about a
    run in *this* fleet: attaching one to anything else creates state that no
    view lists and no ``forget`` can reach, which is a leak rather than a
    feature.

    Both fields ending up empty removes the entry rather than storing a husk, so
    "cleared" and "never set" are the same state on disk. They read the same way
    out, and leaving a record whose only content is a timestamp would show a run
    as described when it is not.
    """
    key = run_key(host, project, slug)
    # The request is validated before state is consulted: whether the text is
    # acceptable has nothing to do with what is tracked, and checking it first
    # keeps the 400 stable regardless of who is looking.
    clean_title = _cleaned(
        title, field="title", limit=MAX_TITLE_CHARS, one_line=True
    )
    clean_description = _cleaned(
        description, field="description", limit=MAX_DESCRIPTION_CHARS, one_line=False
    )
    if clean_title is None and clean_description is None:
        raise MetaError(
            "nothing to set: supply a title, a description, or both. An empty "
            'string ("") clears a field; omitting it leaves it as it is'
        )
    if get_tracked(host, project, slug) is None:
        raise RunNotTracked(
            f"{key} is not tracked by this orchestrator, so there is nothing to "
            "describe. Adopt it first (POST /api/runs/adopt), or spawn it — "
            "metadata is this orchestrator's note about a run in its own fleet."
        )

    # One key changed by rewriting the whole file, so the read and the write are
    # one operation — a title and a description set at the same moment would
    # otherwise keep only whichever landed last.
    with mutating(_meta_path()):
        entries = load_all()
        current = RunMeta.from_dict(key, entries.get(key))
        record = {
            "title": current.title if clean_title is None else clean_title,
            "description": (
                current.description
                if clean_description is None
                else clean_description
            ),
            "updated_at": utc_now_iso(),
        }
        if not (record["title"] or record["description"]):
            entries.pop(key, None)
            _save(entries)
            logger.info("platform_run_meta_cleared key=%s", key)
            return RunMeta()

        entries[key] = record
        _save(entries)
    # Lengths, never the text: this is agent-authored content of arbitrary shape
    # and a log line is not where it belongs.
    logger.info(
        "platform_run_meta_set key=%s title_chars=%d description_chars=%d",
        key, len(record["title"]), len(record["description"]),
    )
    return RunMeta.from_dict(key, record)


def drop(host: str, project: str, slug: str) -> bool:
    """Forget a run's metadata. Returns whether there was any.

    Called by :func:`lmer_platform.runs.forget` rather than by a route of its
    own: metadata is scoped to a tracked run, so the operation that ends the
    tracking is the one that ends this too. Clearing both fields
    (:func:`write` with two empty strings) is the way to empty it without
    untracking the run.
    """
    key = run_key(host, project, slug)
    with mutating(_meta_path()):
        entries = load_all()
        if key not in entries:
            return False
        entries.pop(key)
        _save(entries)
    logger.info("platform_run_meta_dropped key=%s", key)
    return True
