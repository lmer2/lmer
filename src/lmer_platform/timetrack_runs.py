"""The runs time tracking reads: identity, title and issues (issue #665, D21, D22, D26).

An automatic entry belongs to one run, its *identity*. Activity names runs by
:class:`~lmer_timesheet.SessionRef`; a run moving from its slug key to its uid
key (spec §6.1) is still the same run, which :func:`same_run` answers from the
tracked index's ``aliases``. :func:`facts_for` reads what an entry takes from a
run: its title (this orchestrator's metadata, else its slug) and its issues
(an issue or MR URL target first, then the issues set in its metadata).

Every read here is a lock-free JSON snapshot, so it is safe inside the
tracker's store transaction, and none raises: an unreadable index answers
from the references alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from lmer_timesheet import SessionRef, normalise_issue_url
from work_repo.utils import classify_target

from . import meta, runs

logger = logging.getLogger("lmer_platform.timetrack_runs")

__all__ = [
    "RunFacts", "same_run", "canonical_key", "run_keys", "run_issues", "facts_for",
    "facts_for_session",
]

#: The forge routes that name an issue or a merge request: GitLab's ``/-/``
#: routes and GitHub's ``issues`` and ``pull``. A commit or compare URL is a
#: "work-item" to :func:`classify_target` but names no issue (D26).
_ISSUE_ROUTE_RE = re.compile(r"/(?:-/(?:issues|work_items|merge_requests)|issues|pull)/\d+$")


@dataclass(frozen=True)
class RunFacts:
    """What an automatic entry takes from its run.

    ``key`` is the run's canonical key, the one its tracked entry is under now.
    The work key is the first issue, else ``key`` (D25).
    """

    ref: SessionRef
    key: str
    title: str = ""
    issues: tuple = ()

    @property
    def work_key(self) -> str:
        return self.issues[0] if self.issues else self.key


def _tracked(ref: SessionRef) -> Optional[tuple]:
    """``(canonical key, entry)`` of the tracked run *ref* names, or ``None``."""
    try:
        index = runs.load_index()
    except Exception as exc:
        logger.info("platform_time_runs_index_unreadable error=%s", exc)
        return None
    if ref.run and isinstance(index.get(ref.run), dict):
        return ref.run, index[ref.run]
    for key, entry in index.items():
        if not isinstance(entry, dict):
            continue
        if ref.uid and entry.get("uid") == ref.uid:
            return key, entry
        aliases = entry.get("aliases")
        if ref.run and isinstance(aliases, list) and ref.run in aliases:
            return key, entry
    return None


def run_keys(ref: SessionRef) -> tuple:
    """Every key *ref*'s run is known by: its canonical key first, then its aliases.

    An untracked run is known by its own key alone, or by nothing when *ref*
    holds only a uid.
    """
    found = _tracked(ref)
    if found is None:
        return (ref.run,) if ref.run else ()
    key, entry = found
    aliases = entry.get("aliases")
    extra = [alias for alias in aliases if isinstance(alias, str) and alias != key] \
        if isinstance(aliases, list) else []
    if ref.run and ref.run != key and ref.run not in extra:
        extra.append(ref.run)
    return (key, *extra)


def canonical_key(ref: SessionRef) -> str:
    """The key *ref*'s run is tracked under now; its own key when untracked (D25)."""
    keys = run_keys(ref)
    return keys[0] if keys else ""


def same_run(a: Optional[SessionRef], b: Optional[SessionRef]) -> bool:
    """*a* and *b* name one run: the same uid, the same key, or one key an
    alias of the other's run (D21). ``None`` is no run and matches nothing."""
    if a is None or b is None:
        return False
    if a.uid and b.uid:
        return a.uid == b.uid
    if a.run and a.run == b.run:
        return True
    keys_a = set(run_keys(a))
    return bool(keys_a) and bool(keys_a & set(run_keys(b)))


def _issue_of_target(target: Optional[str]) -> Optional[str]:
    if not isinstance(target, str):
        return None
    try:
        url = normalise_issue_url(target)
    except ValueError:
        return None
    if classify_target(url) != "work-item" or not _ISSUE_ROUTE_RE.search(url):
        return None
    return url


def run_issues(target: Optional[str], meta_issues=()) -> tuple:
    """A run's issues: its target when that is an issue or MR URL, then the
    issues set in its metadata, normalised and once each (D26)."""
    issues = []
    first = _issue_of_target(target)
    if first:
        issues.append(first)
    for item in meta_issues or ():
        try:
            url = normalise_issue_url(item)
        except ValueError:
            continue
        if url not in issues:
            issues.append(url)
    return tuple(issues)


def facts_for(ref: Optional[SessionRef]) -> Optional[RunFacts]:
    """What an entry takes from *ref*'s run, or ``None`` when the run is not tracked."""
    if ref is None:
        return None
    found = _tracked(ref)
    if found is None:
        return None
    key, payload = found
    entry = runs.TrackedRun.from_dict(key, payload)
    if entry is None:
        return None
    host, project, segment = entry.host, entry.project, key.rsplit("/", 1)[-1]
    try:
        record = meta.read(host, project, segment)
    except Exception as exc:
        logger.info("platform_time_run_meta_unreadable key=%s error=%s", key, exc)
        record = meta.RunMeta()
    return RunFacts(
        ref=ref,
        key=key,
        title=record.title or entry.slug or segment,
        issues=run_issues(entry.target, record.issues),
    )


def facts_for_session(session_id: str) -> Optional[RunFacts]:
    """:func:`facts_for` of the run *session_id* belongs to; ``None`` when it has none."""
    try:
        run = runs.run_of_session(session_id)
        parts = runs.key_parts_lenient(run) if run else None
        if parts is None:
            return None
        key = runs.run_key(*parts)
        uid = run.get("uid") or ""
        try:
            ref = SessionRef(run=key, uid=uid)
        except ValueError:
            if not uid:
                return None
            ref = SessionRef(uid=uid)
        return facts_for(ref)
    except Exception as exc:
        logger.info("platform_time_run_facts_skipped session=%s error=%s", session_id, exc)
        return None
