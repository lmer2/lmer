"""Has the author moved on the MR since the review was posted? (#401)

The check-in digest (:mod:`lmer_platform.checkin`) names runs nobody has looked at.
A review run that posted its review and wound down sits ``dormant`` at
``in-progress`` whether its author has since pushed or not, so nothing in the run
can say whether a turn is owed — #244's stall and #401's nag look identical from
inside it. The answer lives at the source: has the MR changed since the review?
This module asks, once per check-in window per MR, and remembers the answer in
platform state.

**It fails open.** No token, an HTTP error, a fetcher that crashes, a cache that
will not write, an answer too old to trust: each reads as "the MR may have moved",
and the run stays named. The only thing that takes a run out of the digest is a
fresh, successful fetch showing nothing newer than the review — or an MR that is
merged or closed, on which no review turn can be owed.

**Movement is the author's, not the MR's clock.** Either of:

- a **push**: a diff version created after ``review_posted`` (GitLab records one
  per push, force-pushes included);
- a **comment**: a non-system note created after ``review_posted`` that the
  review run did not write itself.

The MR's ``updated_at`` and a note's ``updated_at`` are deliberately not read.
The review taskdef writes to the MR after ``review_posted`` (it replies "verified
in <sha>" and resolves threads), and a resolve bumps both; GitLab's cross-reference
system notes ("mentioned in !280") bump them too. Read as movement, those named a
review that owed nothing every window until merge (!282, !198, !346, !352 itself).

**Whose comment it is comes from a record, not a clock.** Reviewer and author
are often the same bot account, so identity cannot tell them apart. Nor can the
review session's last event: that is often its teardown, days after its last
write, and an author reply in between would drop the run with a turn owed
(review-mr-149, #244's stall). So ``gitlab-review`` records every MR note a
review session writes as a ``review_note`` event
(:mod:`gitlab_reviewer.review_notes`), the fleet row carries those ids as
``review_note_ids``, and only a comment *not* among them is movement. A reply the
recorder missed — a run from before it, a failed record, a note written another
way — counts as the author's, so the run stays named.

**GitLab MR targets only.** Any other target — a GitHub PR, an issue, free text —
never parses, so its run keeps the digest's ordinary behaviour.

This is the seam, not the whole mechanism: #133's snapshot/diff (every dimension
of an MR, checkpointed) is the later, larger version, and would replace
:func:`fetch_gitlab` and the fields compared here.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import quote

from gitlab_reviewer.review_notes import REVIEW_NOTE_EVENT
from lmer_cli.cli import _parse_gitlab_mr_url
from lmer_cli.tokens import _get_gitlab_token

from .store import StoreError, age_seconds, mutating, read_json, snapshot_path, utc_now_iso, write_json

logger = logging.getLogger("lmer_platform.mr_activity")

__all__ = [
    "REVIEW_POSTED_EVENT", "REVIEW_NOTE_EVENT", "ACTIVITY_FILE", "FETCH_TIMEOUT_SECONDS", "FETCH_WORKERS",
    "NOTES_PAGE", "FINISHED_STATES", "FINAL_STATES", "FetchError", "MrRef",
    "cache_path", "parse_mr", "fetch_gitlab", "read_cache", "refresh",
    "quiet_since_review",
]

#: The run-dir event a review run records once its review is on the MR
#: (``work event review_posted``). Its latest ``ts`` is the time the MR is
#: compared against; the fleet row carries it as ``review_posted_at``.
REVIEW_POSTED_EVENT = "review_posted"

#: ``{"mrs": {"<host>/<project>!<iid>": {"fetched_at", "state", "head_sha",
#: "pushed_at", "comments": [[note id, created_at], ...], "page_floor", "error"}}}``,
#: beside the daemon's other snapshots. Nothing of it goes to the work repo.
ACTIVITY_FILE = "mr_activity.json"

#: Per request. The detector tick waits on it, so it is short.
FETCH_TIMEOUT_SECONDS = 10

#: MRs read side by side in one refresh. The refresh runs inside the detector
#: tick, ahead of question and crash delivery: one request after another cost
#: 133s for 170 MRs. Small enough not to lean on a GitLab instance.
FETCH_WORKERS = 8

#: Newest notes read per MR. System notes are skipped on this side (the notes API
#: cannot filter them). A full page cannot see older comments, so it reports how
#: far back it reached (``page_floor``), and a page that does not reach back to
#: the review is not believed.
NOTES_PAGE = 100

#: Largest API answer read. An MR object is a few KB; this is a bound, not a size.
MAX_RESPONSE_BYTES = 1_000_000

#: MR states on which no review turn can be owed.
FINISHED_STATES = ("merged", "closed")

#: Finished states GitLab cannot leave: an answer in one never goes stale, so it
#: is never fetched again (a closed MR can be reopened, so it is not here).
FINAL_STATES = ("merged",)


class FetchError(Exception):
    """The MR could not be read. Its message never carries the token.

    *host_wide* when the failure speaks for every MR on the host — no token, a
    rejected token, a host that cannot be reached — so a refresh stops asking it.
    """

    def __init__(self, message: str, *, host_wide: bool = False) -> None:
        super().__init__(message)
        self.host_wide = host_wide


@dataclass(frozen=True)
class MrRef:
    host: str
    project: str
    iid: str
    scheme: str = "https"

    @property
    def key(self) -> str:
        return f"{self.host}/{self.project}!{self.iid}"

    @property
    def api_url(self) -> str:
        return (
            f"{self.scheme}://{self.host}/api/v4/projects/"
            f"{quote(self.project, safe='')}/merge_requests/{self.iid}"
        )


def parse_mr(target: object) -> Optional[MrRef]:
    """The GitLab MR a run's target names, or ``None`` for anything else."""
    if not isinstance(target, str) or not target:
        return None
    host, project, iid = _parse_gitlab_mr_url(target)
    if not (host and project and iid):
        return None
    scheme = "http" if target.lower().startswith("http://") else "https"
    return MrRef(host=host, project=project, iid=iid, scheme=scheme)


def _get_json(url: str, token: str, urlopen, timeout: float):
    request = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(MAX_RESPONSE_BYTES))
    except urllib.error.HTTPError as exc:
        raise FetchError(
            f"HTTP {exc.code} from {url}", host_wide=exc.code in (401, 403)
        ) from None
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{type(exc).__name__} reading {url}", host_wide=True) from None
    except ValueError:
        raise FetchError(f"unreadable answer from {url}") from None


def fetch_gitlab(
    ref: MrRef,
    *,
    token_for: Callable[[str], Optional[str]] = _get_gitlab_token,
    urlopen=urllib.request.urlopen,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> dict:
    """``{"state", "head_sha", "pushed_at", "comments", "page_floor"}`` for *ref*.

    The MR itself, then — only while it is open — its newest diff version (the
    last push) and its newest page of notes: ``comments`` is ``[note id,
    created_at]`` for each non-system note on it, newest first, and
    ``page_floor`` is the oldest note's ``created_at`` when the page was full
    (``None`` when it held the whole history). Raises :class:`FetchError` for
    anything short of every answer.
    """
    token = token_for(ref.host)
    if not token:
        raise FetchError(f"no API token for {ref.host}", host_wide=True)
    mr = _get_json(ref.api_url, token, urlopen, timeout)
    if not isinstance(mr, dict):
        raise FetchError(f"not an MR object from {ref.api_url}")
    reported = {
        "state": mr.get("state"),
        "head_sha": mr.get("sha"),
        "pushed_at": None,
        "comments": [],
        "page_floor": None,
    }
    if reported["state"] in FINISHED_STATES:
        return reported
    versions = _get_json(f"{ref.api_url}/versions?per_page=1", token, urlopen, timeout)
    if not isinstance(versions, list):
        raise FetchError(f"not a version list from {ref.api_url}/versions")
    if versions and isinstance(versions[0], dict):
        reported["pushed_at"] = versions[0].get("created_at")
    notes = _get_json(
        f"{ref.api_url}/notes?sort=desc&order_by=created_at&per_page={NOTES_PAGE}",
        token, urlopen, timeout,
    )
    if not isinstance(notes, list):
        raise FetchError(f"not a note list from {ref.api_url}/notes")
    reported["comments"] = [
        [note.get("id"), note.get("created_at")]
        for note in notes if isinstance(note, dict) and not note.get("system")
    ]
    if len(notes) >= NOTES_PAGE:
        oldest = notes[-1] if isinstance(notes[-1], dict) else {}
        reported["page_floor"] = oldest.get("created_at") or "unknown"
    return reported


def cache_path() -> Path:
    return snapshot_path(ACTIVITY_FILE)


def read_cache() -> dict:
    """The cache, or ``{"mrs": {}}`` when there is no usable file. Never raises."""
    try:
        stored = read_json(cache_path())
    except StoreError as exc:
        logger.warning("platform_mr_activity_unreadable error=%s — every review run "
                       "reads as moved until the next fetch", exc)
        return {"mrs": {}}
    mrs = stored.get("mrs") if isinstance(stored, dict) else None
    if not isinstance(mrs, dict):
        return {"mrs": {}}
    return {"mrs": {key: entry for key, entry in mrs.items() if isinstance(entry, dict)}}


def _write_cache(cache: dict) -> None:
    with mutating(cache_path()):
        write_json(cache_path(), cache)


def _fresh(entry: dict, max_age: float) -> bool:
    age = age_seconds(entry.get("fetched_at"))
    return age is not None and -60 <= age < max_age


def _final(entry: dict) -> bool:
    """A successful answer that can never change (a merged MR)."""
    return not entry.get("error") and entry.get("state") in FINAL_STATES


def refresh(
    candidates: Iterable[tuple],
    *,
    window: int,
    fetch: Optional[Callable[[MrRef], dict]] = None,
) -> dict:
    """Fetch every candidate's MR whose answer is a window old, and return the cache.

    *candidates* is ``(run key, target)`` per review run that would otherwise be
    named (:func:`lmer_platform.checkin.review_candidates`). One fetch per MR
    however many runs name it, none while the last answer — success or failure —
    is younger than *window* (so a failing host is asked once a window rather than
    once a tick), and never again once the MR is merged. The fetches that are due
    run side by side, at most :data:`FETCH_WORKERS` at once, and a host that fails
    host-wide is not asked about its remaining MRs this refresh. MRs no candidate
    names are dropped.

    Never raises: a run whose MR could not be read is simply not quiet.
    """
    fetch = fetch or fetch_gitlab
    previous = read_cache()["mrs"]
    mrs: dict = {}
    due: dict = {}
    for candidate in candidates:
        ref = parse_mr(candidate[1])
        if ref is None or ref.key in mrs or ref.key in due:
            continue
        entry = previous.get(ref.key)
        if entry is not None and (_final(entry) or _fresh(entry, window)):
            mrs[ref.key] = dict(entry)
        else:
            due[ref.key] = ref
    mrs.update(_fetch_all(list(due.values()), fetch))
    cache = {"mrs": mrs}
    if cache != {"mrs": previous}:
        try:
            _write_cache(cache)
        except StoreError as exc:
            logger.warning("platform_mr_activity_unwritable error=%s — this tick's "
                           "answers are used, and fetched again next tick", exc)
    return cache


def _fetch_all(refs: list, fetch) -> dict:
    """``{ref.key: entry}`` for *refs*, read side by side (:data:`FETCH_WORKERS`)."""
    if not refs:
        return {}
    down: dict = {}
    lock = threading.Lock()

    def one(ref: MrRef) -> dict:
        with lock:
            skipped = down.get(ref.host)
        if skipped is not None:
            return {"fetched_at": utc_now_iso(), "error": skipped}
        entry, host_wide = _fetched(ref, fetch)
        if host_wide:
            with lock:
                down.setdefault(ref.host, entry["error"])
        return entry

    workers = max(1, min(FETCH_WORKERS, len(refs)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mr-activity") as pool:
        return dict(zip((ref.key for ref in refs), pool.map(one, refs)))


def _fetched(ref: MrRef, fetch) -> tuple:
    """``(entry, host_wide)``: one MR's answer, or its failure as an entry."""
    now = utc_now_iso()
    try:
        reported = fetch(ref)
        if not isinstance(reported, dict):
            raise FetchError(f"fetcher returned {type(reported).__name__}")
    except FetchError as exc:
        logger.info("platform_mr_activity_fetch_failed mr=%s error=%s — its review "
                    "runs stay named", ref.key, exc)
        return {"fetched_at": now, "error": str(exc)}, exc.host_wide
    except Exception as exc:  # noqa: BLE001 - fail open, never cost the digest
        logger.warning("platform_mr_activity_fetch_crashed mr=%s error=%s: %s",
                       ref.key, type(exc).__name__, exc)
        return {"fetched_at": now, "error": f"{type(exc).__name__}: {exc}"}, False
    entry = {
        field: reported.get(field)
        for field in ("state", "head_sha", "pushed_at", "comments", "page_floor")
    }
    entry["fetched_at"] = now
    return entry, False


def _when(stamp: object) -> Optional[datetime]:
    """An ISO-8601 time, with or without fractions or ``Z``; ``None`` otherwise."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def quiet_since_review(
    cache: Optional[dict],
    target: object,
    review_posted_at: object,
    *,
    own_note_ids: Iterable = (),
    max_age: float,
) -> bool:
    """Whether the author has positively not moved since *review_posted_at*.

    Not moved: no push after *review_posted_at*, and no comment after it other
    than the review's own (*own_note_ids*, the ``review_note`` ids the run
    recorded). A merged MR is final; a closed one is believed while fresh.

    ``False`` — the run stays named — for everything that is not a successful
    answer fresh enough to trust: no cache, no entry, an error, an entry older
    than *max_age*, a time that will not parse, a page of notes that does not
    reach back to the review.
    """
    ref = parse_mr(target)
    posted = _when(review_posted_at)
    if ref is None or posted is None or not isinstance(cache, dict):
        return False
    entry = (cache.get("mrs") or {}).get(ref.key)
    if not isinstance(entry, dict) or entry.get("error"):
        return False
    if _final(entry):
        return True
    if not _fresh(entry, max_age):
        return False
    if entry.get("state") in FINISHED_STATES:
        return True
    pushed = _when(entry.get("pushed_at"))
    if pushed is None or pushed > posted:
        return False
    if entry.get("page_floor") is not None:
        floor = _when(entry.get("page_floor"))
        if floor is None or floor > posted:
            return False
    comments = entry.get("comments")
    if not isinstance(comments, list):
        return False
    own = {note_id for note_id in (own_note_ids or ()) if isinstance(note_id, int)}
    for comment in comments:
        if not isinstance(comment, list) or len(comment) != 2:
            return False
        note_id, created = comment
        when = _when(created)
        if when is None:
            return False
        if when > posted and note_id not in own:
            return False
    return True
