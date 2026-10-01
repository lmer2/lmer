"""Shared schema-2 release claims, published with the CLI's Git CAS.

Timestamps are informational: only the holder or an explicit operator
decision releases a claim. Callers must publish via claim-by-push before
reporting ownership; these helpers change local files only.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from . import run_state, session_record
from .utils import work_repo_layout
from .transactions import mutation_transaction


def claim_path() -> Path:
    layout = work_repo_layout()
    if layout.base is None or not run_state.is_process_record():
        raise run_state.RunStateError("release claims require a schema-2 project")
    return layout.base / "_claims" / "release.yaml"


def claim_rel_path() -> str:
    return claim_path().relative_to(work_repo_layout().root).as_posix()


#: What an absent claim file means: the resource is free.
UNCLAIMED = {"schema": 1, "holder": None}


def _validated(claim, source: str) -> dict:
    if (
        not isinstance(claim, dict)
        or type(claim.get("schema")) is not int
        or claim["schema"] != 1
        or "holder" not in claim
        or (claim.get("holder") is not None and (
            not isinstance(claim["holder"], str)
            or not session_record.UID_RE.fullmatch(claim["holder"])
        ))
    ):
        raise run_state.RunStateError(f"invalid release claim: {source}")
    return claim


def read_claim(path: Path | None = None) -> dict:
    """Read local state; an explicit path supports read-only validation."""
    path = claim_path() if path is None else path
    if not path.exists():
        return dict(UNCLAIMED)
    try:
        claim = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise run_state.RunStateError(f"unreadable release claim: {path}") from exc
    return _validated(claim, str(path))


def parse_claim(text: str, source: str) -> dict:
    """The claim document a published tree holds, validated like a local one."""
    try:
        claim = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise run_state.RunStateError(f"unreadable release claim: {source}") from exc
    return _validated(claim, source)


def serialize_claim(claim: dict) -> str:
    """One spelling of a claim document, for the file and for the blob."""
    return yaml.safe_dump(claim, sort_keys=False)


def available(claim: dict, uid: str, *, force: bool = False) -> dict:
    """Refuse a transition a foreign holder owns; no age expiry, by design."""
    if claim.get("holder") not in (None, uid) and not force:
        raise run_state.RunStateError(
            f"release is held by session {claim['holder']}; inspect that "
            "session or explicitly force-unclaim after an operator decision"
        )
    return claim


def require_available(uid: str, *, force: bool = False) -> dict:
    return available(read_claim(), uid, force=force)


def next_claim(current: dict, uid: str, *, release: bool = False, force: bool = False) -> dict:
    """The claim document after this transition, preserving unknown fields.

    Pure: *current* is whatever the caller arbitrated against — for the shared
    CAS that is the freshly fetched PUBLISHED claim, never the checkout's
    possibly-stale copy of it.
    """
    if not session_record.UID_RE.fullmatch(uid):
        raise run_state.RunStateError("release claims require a session UID")
    claim = dict(available(current, uid, force=force and release))
    if uid != run_state.current_session_id():
        raise run_state.RunStateError("only the current session can change a claim")
    claim["holder"] = None if release else uid
    claim["updated"] = run_state.utc_now_iso()
    return claim


@mutation_transaction
def change_claim(uid: str, *, release: bool = False, force: bool = False) -> dict:
    """Prepare a transition in the checkout (the abort/session-end verbs)."""
    claim = next_claim(read_claim(), uid, release=release, force=force)
    path = claim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialize_claim(claim))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return claim
