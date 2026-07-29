"""Release-run memory kernel (release taskdef; masterplan release-flow §3).

One release run = one release.yaml in the run dir, holding everything leg 1
and leg 2 record and everything a relaunch re-derives from: the release
version, the bump-MR merge SHA, the release-MR merge SHA, the signed-tag
receipt, and the push/upload receipts (GitHub main push, GitHub tag push,
the Actions run that actually uploaded, the PyPI URL, the GitLab tag push).
The pure derive_leg()/next_step() answer "bump merged? release MR merged?
tag created? pushed where? which Actions run actually uploaded?" from
recorded state alone, so a relaunched session resumes at exactly one next
action without re-reading remotes (spec §3: watch is best-effort, resume is
the contract).

STORAGE DECISION: a dedicated single-writer release.yaml, NOT additive keys
in state.yaml. Same safety contract as ledger.yaml (the shared
_load_yaml_mapping preamble in run_state): atomic tmp+rename writes, corrupt
files backed up as release.yaml.bad-<stamp>, newer-schema read-only refusal.
Rationale: state.yaml is the universal run contract every taskdef reads and
writes — folding release-only keys in would couple this record's schema
evolution to SCHEMA_VERSION and put it in the blast radius of state.yaml's
backup-and-recover cycle (ensure_run reseeds a corrupt state file IN PLACE,
which would silently drop an embedded release record mid-release). A
sibling file is crash-isolated, versions independently
(RELEASE_SCHEMA_VERSION), and follows the precedent ledger.yaml set for a
per-purpose schema'd file beside state.yaml.

HARD STOPS (kernel-level errors, spec §3 leg 2 + §7 — enforced here, not
just in taskdef instructions):
- The version read from pyproject.toml AT the release-MR merge SHA must
  equal leg 1's recorded version (mismatch = a second bump or foreign
  commit landed — human decision required).
- A recorded tag must point at exactly the recorded merge SHA and be named
  v<version> (never re-point, never re-sign; the tag-`0.2.0`-without-prefix
  damage from spec §1 is refused at record time).
Identity fields (version, merge SHAs, tag) are write-once: re-recording the
same value is an idempotent no-op (re-entered legs converge), a different
value is an error. Receipts MAY be re-recorded — a re-dispatched Actions
run must be able to replace the URL with the run that ACTUALLY uploaded
(spec §7 re-run artifact drift) — and every mutation appends a `release`
event to events.jsonl, so prior values stay in the audit trail.

FROZEN `work` CLI verb names (R5: taskdef bodies cite these before the CLI
verbs land; same family as the frozen claim verbs `work release claim` /
`work release claim-status` / `work release unclaim`):

    work release record version <X.Y.Z>
    work release record bump-sha <sha>
    work release record merge-sha <sha> --version <observed-version>
    work release record tag <vX.Y.Z> --sha <sha>
    work release record receipt <name> [--url <url>] [--note "..."]
        <name> is one of: github-main-push, github-tag-push, actions-run,
        pypi, gitlab-tag-push (--url required for actions-run and pypi:
        receipts must record which run/URL actually uploaded)
    work release abort [--reason "..."]
    work release status [--json]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from .run_state import (
    RunStateError,
    _backup_bad_state,
    _load_yaml_mapping,
    append_event,
    slug_available,
    utc_now_iso,
)
from .utils import redact_secrets

RELEASE_FILE = "release.yaml"
RELEASE_SCHEMA_VERSION = 1
# Frozen receipt names (R5) — the leg-2 ladder in spec §3 order. actions-run
# and pypi carry a required URL: spec §7's re-run artifact-drift caveat means
# the receipt must record which Actions run ACTUALLY uploaded, never just
# "the last green one".
RECEIPT_NAMES = (
    "github-main-push",
    "github-tag-push",
    "actions-run",
    "pypi",
    "gitlab-tag-push",
)
URL_REQUIRED_RECEIPTS = ("actions-run", "pypi")
# Frozen next_step names, in ladder order — the resume decision table
# (release-resume partial) cites these verbatim.
STEPS = (
    "leg1-bump",
    "leg1-record-bump-merge",
    "gate-await-release-merge",
    "leg2-create-tag",
    "leg2-push-github-main",
    "leg2-push-github-tag",
    "leg2-poll-actions",
    "leg2-record-pypi",
    "leg2-push-gitlab-tag",
    "complete",
)
LEGS = ("leg1", "gate", "leg2", "complete")
# Terminal abort (spec §7's abandoned release) — deliberately NOT a
# STEPS/LEGS ladder entry: those are the frozen resume-row names the
# decision table cites one row per step, and an aborted run is not a
# resume row (state.yaml is complete/aborted; session-start's
# completed-run directive owns it). derive_leg reports this leg with
# next_step None — nothing to advance.
LEG_ABORTED = "aborted"
# Every step keys on the recorded merge SHA (spec §3 leg 2), so recorded
# SHAs must be full and exact — never a short form that could alias.
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
# Bounded so a pathological runs/ tree cannot spin the disambiguation loop in
# unique_release_slug; reaching it would need this many runs already holding
# one stamped address.
_UNIQUE_SLUG_ATTEMPTS = 100


class ReleaseRunError(RunStateError):
    """Raised on release-record hard stops and unsafe reads/writes."""


def release_slug(base_slug: str, version: Optional[str] = None) -> str:
    """The version-bearing slug a release run moves to (run_state.reslug_run).

    `derive_slug()` is deterministic per `(taskdef, target)`, so without a
    version every release of a repository resolved to one run dir and the
    second release was refused forever. Recording the version gives the run
    an address of its own — `release-global` → `release-global-v0.6.0` —
    and frees the bare one for the next release.

    The `v` mirrors the tag name; the RECORD still holds the bare `0.6.0`
    (_valid_version refuses a `v`-prefixed version, unchanged). A run that
    goes terminal without ever recording a version — an abandoned release
    aborted before leg 1 — has no version to name it with, so it takes a
    compact-UTC stamp instead: unique, sortable, and never mistakable for a
    version (`release-global-20260728T031122Z`).
    """
    if version:
        return f"{base_slug}-v{_valid_version(version)}"
    stamp = utc_now_iso().replace("-", "").replace(":", "")
    return f"{base_slug}-{stamp}"


def names_version(slug: Optional[str], base_slug: str, version: str) -> bool:
    """True when `slug` is an address minted for `base_slug` + `version`.

    A version REPEATS: `RELEASE-FLOW.md` §6 leaves a declined release's bump
    on prep-release, so the successor's dry-run skips it and the successor
    records the same `X.Y.Z`. Its address therefore cannot always be the
    canonical `<base>-v<X.Y.Z>` — the declined run may still be parked there
    — so unique_release_slug hands out a variant instead.

    `_ensure_release_slug` runs after EVERY release-record verb, and it
    decides "already moved?" by asking this rather than by string equality
    with the canonical form. Without that, a run sitting on a variant would
    look un-moved at every verb and re-slug itself to a fresh address each
    time.
    """
    if not slug:
        return False
    canonical = f"{base_slug}-v{_valid_version(version)}"
    return slug == canonical or slug.startswith(f"{canonical}-")


def unique_release_slug(
    base_slug: str, version: Optional[str] = None, base_dir=None, rdir=None
) -> str:
    """An address for this release run that no other run holds.

    `release_slug` names the address a run WANTS; this returns one it can
    actually take. The distinction is the iteration-6 defect: when the
    version-bearing address was occupied the re-slug simply gave up, the run
    stayed on the seed address it was supposed to vacate, and the NEXT
    release found a terminal run there and was refused forever — the wedge
    version-in-slug exists to close. An address that is always free by
    construction removes the whole class.

    The canonical `<base>-v<X.Y.Z>` when it is free, else the stamped form
    `<base>-v<X.Y.Z>-<compact-UTC>` (release_slug's existing "no address of
    its own" spelling, applied to the version-bearing base so the version
    stays legible), else that plus a counter. Occupancy is
    `run_state.slug_available`: BOTH the recorded slug and the dirs that
    address it, since a named run takes one while leaving the other free.

    `rdir` is the run being moved, which never counts as occupying the
    address it is trying to take — otherwise a re-slug interrupted between
    its rename and its slug write would find "its own" address taken and
    drift to a fresh one instead of completing the move it started.

    The guarantee is TOTAL: exhausting the bounded search raises rather than
    handing back an address `slug_available` has just refused. Returning a
    taken address would drop the caller straight back into the skipped-re-slug
    wedge this function exists to remove, and both callers already have a
    correct answer for "no address available" — the claim's roll-over refuses
    (fail-closed) and the record verb leaves the run where it is (healed at
    the next verb, exactly as a failed rename is).
    """
    wanted = release_slug(base_slug, version)
    if slug_available(wanted, base_dir, exclude=rdir):
        return wanted
    stamped = release_slug(wanted, None)
    if slug_available(stamped, base_dir, exclude=rdir):
        return stamped
    for n in range(2, _UNIQUE_SLUG_ATTEMPTS):
        candidate = f"{stamped}-{n}"
        if slug_available(candidate, base_dir, exclude=rdir):
            return candidate
    raise ReleaseRunError(
        f"no free release address for '{wanted}': every candidate through "
        f"'{stamped}-{_UNIQUE_SLUG_ATTEMPTS - 1}' is already held by a run"
    )


def seed_release() -> dict:
    """Fresh release record: nothing merged, nothing tagged, nothing pushed."""
    return {
        "schema": RELEASE_SCHEMA_VERSION,
        "version": None,
        "bump_mr_merge_sha": None,
        "release_mr_merge_sha": None,
        "tag": None,
        "receipts": {},
    }


def load_release(rdir: Path) -> Optional[dict]:
    """Read release.yaml. None if absent. Same safety contract as ledger.yaml
    (the shared preamble): corrupt files are backed up first (error raised),
    a newer schema is a read-only refusal, and non-mapping `receipts`/`tag`/
    `aborted` fields count as corrupt."""
    path = rdir / RELEASE_FILE
    if not path.exists():
        return None
    release = _load_yaml_mapping(path, "release", RELEASE_SCHEMA_VERSION)
    receipts = release.get("receipts")
    if receipts is None:
        release["receipts"] = {}
    elif not isinstance(receipts, dict):
        raise _backup_bad_state(path, f"{path.name} receipts field is not a mapping")
    tag = release.get("tag")
    if tag is not None and not isinstance(tag, dict):
        raise _backup_bad_state(path, f"{path.name} tag field is not a mapping")
    aborted = release.get("aborted")
    if aborted is not None and not isinstance(aborted, dict):
        raise _backup_bad_state(path, f"{path.name} aborted field is not a mapping")
    return release


def write_release(rdir: Path, release: dict) -> None:
    """Atomic tmp+rename write. The ONLY writer of release.yaml (single-writer
    discipline, same as state.yaml/ledger.yaml)."""
    rdir.mkdir(parents=True, exist_ok=True)
    release = dict(release)
    release["updated"] = utc_now_iso()
    tmp = rdir / f".{RELEASE_FILE}.tmp"
    tmp.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")
    tmp.replace(rdir / RELEASE_FILE)


def _valid_sha(sha, what: str) -> str:
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha.strip()):
        raise ReleaseRunError(f"{what} must be a full 40-hex commit SHA (got {sha!r})")
    return sha.strip().lower()


def _valid_version(version) -> str:
    if not isinstance(version, str) or not version.strip() or " " in version.strip():
        raise ReleaseRunError(f"release version must be a non-empty string (got {version!r})")
    version = version.strip()
    if version.startswith("v"):
        # The record holds the pyproject version; the TAG adds the prefix.
        # Accepting "v0.5.0" here would make the derived tag "vv0.5.0".
        raise ReleaseRunError(
            f"release version must not carry the tag prefix (got {version!r} — "
            f"record {version[1:]!r}; the tag name adds the 'v')"
        )
    return version


def _already_recorded(existing, new, what: str) -> bool:
    """Write-once guard for identity fields: True = same value already there
    (idempotent no-op), False = record it. A DIFFERENT recorded value is a
    hard stop — recorded release identity never silently moves."""
    if existing is None:
        return False
    if existing == new:
        return True
    raise ReleaseRunError(
        f"{what} already recorded as {existing!r} — refusing to change it to {new!r}"
    )


def _load_or_seed(rdir: Path) -> dict:
    return load_release(rdir) or seed_release()


def record_version(rdir: Path, version: str) -> dict:
    """Record leg 1's release version (`work release record version`).
    Write-once; re-recording the same version is a no-op (the abandoned-
    release resume path re-runs leg 1 against an already-bumped branch)."""
    version = _valid_version(version)
    release = _load_or_seed(rdir)
    if _already_recorded(release.get("version"), version, "release version"):
        return release
    release["version"] = version
    write_release(rdir, release)
    append_event(
        rdir, "release", note=f"version: {version}",
        data={"field": "version", "version": version},
    )
    return release


def record_bump_merge(rdir: Path, sha: str) -> dict:
    """Record the bump-MR merge SHA (`work release record bump-sha`) —
    leg 1's completion marker (spec §3 leg 1 step 4). Requires the version
    to be recorded first: the two land together at bump-MR merge, and a
    bump SHA without a version would leave derive_leg unable to name the
    release the gate is watching for."""
    sha = _valid_sha(sha, "bump-MR merge SHA")
    release = _load_or_seed(rdir)
    if release.get("version") is None:
        raise ReleaseRunError("no release version recorded — record the version first")
    if _already_recorded(release.get("bump_mr_merge_sha"), sha, "bump-MR merge SHA"):
        return release
    release["bump_mr_merge_sha"] = sha
    write_release(rdir, release)
    append_event(
        rdir, "release", note=f"bump-sha: {sha[:12]}",
        data={"field": "bump_mr_merge_sha", "sha": sha},
    )
    return release


def record_release_merge(rdir: Path, sha: str, version_at_sha: str) -> dict:
    """Record the release-MR merge SHA (`work release record merge-sha`) —
    the SHA every leg-2 step keys on. `version_at_sha` is the version the
    caller read from pyproject.toml AT that SHA; HARD STOP when it disagrees
    with leg 1's recorded version (spec §3 leg 2 step 1: a second bump or
    foreign commit landed — human decision required). The check runs even on
    an idempotent re-record — a re-entered leg 2 must re-prove the binding,
    not coast on the earlier write."""
    sha = _valid_sha(sha, "release-MR merge SHA")
    release = _load_or_seed(rdir)
    recorded = release.get("version")
    if recorded is None:
        raise ReleaseRunError("no release version recorded — record leg 1 first")
    if release.get("bump_mr_merge_sha") is None:
        raise ReleaseRunError("no bump-MR merge SHA recorded — record leg 1 first")
    if version_at_sha != recorded:
        raise ReleaseRunError(
            f"HARD STOP: version at release-MR merge SHA {sha[:12]} is "
            f"{version_at_sha!r} but leg 1 recorded {recorded!r} — a second bump "
            f"or foreign commit landed; human decision required"
        )
    if _already_recorded(release.get("release_mr_merge_sha"), sha, "release-MR merge SHA"):
        return release
    release["release_mr_merge_sha"] = sha
    write_release(rdir, release)
    append_event(
        rdir, "release", note=f"merge-sha: {sha[:12]}",
        data={"field": "release_mr_merge_sha", "sha": sha, "version_at_sha": version_at_sha},
    )
    return release


def record_tag(rdir: Path, name: str, sha: str) -> dict:
    """Record the signed-tag creation receipt (`work release record tag`).
    HARD STOPS: the tag SHA must equal the recorded release-MR merge SHA
    (never re-point), the name must be exactly v<recorded version> (the
    spec §1 `0.2.0`-without-prefix damage is refused here), and a tag
    already recorded with a different name/SHA is an error (never re-sign).
    Re-recording the identical tag is a no-op — re-entered leg 2."""
    sha = _valid_sha(sha, "tag SHA")
    release = _load_or_seed(rdir)
    merge_sha = release.get("release_mr_merge_sha")
    if merge_sha is None:
        raise ReleaseRunError("no release-MR merge SHA recorded — record it first")
    version = release.get("version")
    expected = f"v{version}"
    if name != expected:
        raise ReleaseRunError(
            f"HARD STOP: tag name {name!r} does not match recorded version "
            f"{version!r} (expected {expected!r}) — the tag name is always "
            f"v<version> at the tagged commit"
        )
    if sha != merge_sha:
        raise ReleaseRunError(
            f"HARD STOP: tag SHA {sha[:12]} disagrees with the recorded "
            f"release-MR merge SHA {merge_sha[:12]} — never re-point, never re-sign"
        )
    existing = release.get("tag")
    if isinstance(existing, dict):
        if existing.get("name") == name and existing.get("sha") == sha:
            return release
        raise ReleaseRunError(
            f"HARD STOP: tag already recorded as {existing.get('name')!r} @ "
            f"{str(existing.get('sha'))[:12]} — never re-point, never re-sign"
        )
    release["tag"] = {"name": name, "sha": sha, "created": utc_now_iso()}
    write_release(rdir, release)
    append_event(
        rdir, "release", note=f"tag: {name} @ {sha[:12]}",
        data={"field": "tag", "tag": name, "sha": sha},
    )
    return release


def record_receipt(
    rdir: Path,
    name: str,
    url: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Record a push/upload receipt (`work release record receipt`). `name`
    is one of RECEIPT_NAMES; actions-run and pypi require `url` (the receipt
    must record which Actions run actually uploaded / where PyPI serves the
    version — spec §7). Unlike the identity fields, receipts may be
    re-recorded: a re-dispatched Actions run replaces the URL, and the prior
    value stays in the `release` audit event trail."""
    if name not in RECEIPT_NAMES:
        raise ReleaseRunError(
            f"unknown receipt {name!r} (expected one of {', '.join(RECEIPT_NAMES)})"
        )
    if name in URL_REQUIRED_RECEIPTS and not url:
        raise ReleaseRunError(
            f"receipt {name!r} requires --url: it must record which run/URL "
            f"actually uploaded, never be a bare checkmark"
        )
    release = _load_or_seed(rdir)
    if release.get("tag") is None:
        raise ReleaseRunError("no tag recorded — nothing can have been pushed yet")
    row: dict = {"recorded": utc_now_iso()}
    # Free-text lands in the (shared) work repo — redact like the other
    # agent-typed writers do. URLs too: a token-bearing URL must never land.
    if url is not None:
        row["url"] = redact_secrets(url)
    if note is not None:
        row["note"] = redact_secrets(note)
    release["receipts"] = dict(release.get("receipts") or {})
    release["receipts"][name] = row
    write_release(rdir, release)
    data = {"field": "receipt", "receipt": name}
    for key, value in (("url", row.get("url")), ("note", row.get("note"))):
        if value is not None:
            data[key] = value
    append_event(rdir, "release", note=f"receipt: {name}", data=data)
    return release


def record_abort(rdir: Path, reason: Optional[str] = None) -> dict:
    """Mark the release record terminal (`work release abort` — spec §7's
    abandoned release: bump merged, human declines the release MR).

    Writes the `aborted` block ({at, reason}) and NOTHING else: every
    identity field and receipt already recorded stays — above all the
    bump-MR merge SHA, which the next release run's ctl dry-run needs so
    it can see the version already bumped on `prep-release` and skip the
    bump (the bump commit stays; leg 1 remains re-derivable). derive_leg()
    on an aborted record reports LEG_ABORTED with next_step None — nothing
    to advance. Terminal and idempotent: a re-abort is a no-op preserving
    the first record (no write, no audit noise). The state.yaml half of
    the abort (status/stop_reason/claim) is run_state.abort_run's — the
    CLI verb composes the two."""
    release = _load_or_seed(rdir)
    if isinstance(release.get("aborted"), dict):
        return release
    row: dict = {"at": utc_now_iso()}
    if reason is not None:
        # Free-text lands in the (shared) work repo — redact like the
        # other agent-typed writers do.
        row["reason"] = redact_secrets(reason)
    release["aborted"] = row
    write_release(rdir, release)
    data: dict = {"field": "aborted"}
    if "reason" in row:
        data["reason"] = row["reason"]
    append_event(rdir, "release", note="aborted", data=data)
    return release


def _receipt_url(receipts: dict, name: str) -> Optional[str]:
    row = receipts.get(name)
    return row.get("url") if isinstance(row, dict) else None


def derive_leg(release: Optional[dict]) -> dict:
    """Pure leg/next-step derivation from recorded state alone (spec §3:
    "relaunching the release taskdef re-derives the leg from run state and
    continues"). No fs, no env, no remotes — fully unit-testable.

    Walks the ladder in spec order and stops at the FIRST missing record, so
    a re-entered leg 2 lands mid-ladder and out-of-order receipts still
    converge (e.g. a gitlab-tag-push recorded without an actions-run receipt
    derives to leg2-poll-actions, not past it). Raises ReleaseRunError when
    the recorded state is internally inconsistent (tag SHA vs merge SHA, tag
    name vs version) — the record functions refuse to write those, so seeing
    one means the file was hand-edited: hard stop, never converge over it."""
    if not isinstance(release, dict):
        release = {}
    version = release.get("version")
    bump_sha = release.get("bump_mr_merge_sha")
    merge_sha = release.get("release_mr_merge_sha")
    tag = release.get("tag")
    tag = tag if isinstance(tag, dict) else None
    receipts = release.get("receipts")
    receipts = receipts if isinstance(receipts, dict) else {}
    aborted = release.get("aborted")
    aborted = aborted if isinstance(aborted, dict) else None
    if tag is not None:
        if merge_sha is not None and tag.get("sha") != merge_sha:
            raise ReleaseRunError(
                f"HARD STOP: recorded tag SHA {str(tag.get('sha'))[:12]} disagrees "
                f"with the recorded merge SHA {merge_sha[:12]} — never re-point"
            )
        if version is not None and tag.get("name") != f"v{version}":
            raise ReleaseRunError(
                f"HARD STOP: recorded tag {tag.get('name')!r} does not match "
                f"recorded version {version!r}"
            )
    if aborted is not None:
        # Terminal abort (spec §7): the record is closed — no ladder walk,
        # next_step None says "nothing to advance" to a scheduled relaunch.
        # The recorded fields below still carry, so leg 1 stays
        # re-derivable (bump_merged True → the next run skips the bump).
        leg, step = LEG_ABORTED, None
    elif version is None:
        leg, step = "leg1", "leg1-bump"
    elif bump_sha is None:
        leg, step = "leg1", "leg1-record-bump-merge"
    elif merge_sha is None:
        leg, step = "gate", "gate-await-release-merge"
    elif tag is None:
        leg, step = "leg2", "leg2-create-tag"
    elif "github-main-push" not in receipts:
        leg, step = "leg2", "leg2-push-github-main"
    elif "github-tag-push" not in receipts:
        leg, step = "leg2", "leg2-push-github-tag"
    elif "actions-run" not in receipts:
        leg, step = "leg2", "leg2-poll-actions"
    elif "pypi" not in receipts:
        leg, step = "leg2", "leg2-record-pypi"
    elif "gitlab-tag-push" not in receipts:
        leg, step = "leg2", "leg2-push-gitlab-tag"
    else:
        leg, step = "complete", "complete"
    return {
        "leg": leg,
        "next_step": step,
        "aborted": aborted is not None,
        "abort_reason": aborted.get("reason") if aborted else None,
        "version": version,
        "bump_merged": bump_sha is not None,
        "release_merged": merge_sha is not None,
        "merge_sha": merge_sha,
        "tag_created": tag is not None,
        "tag": tag.get("name") if tag else None,
        "pushed": {
            "github_main": "github-main-push" in receipts,
            "github_tag": "github-tag-push" in receipts,
            "gitlab_tag": "gitlab-tag-push" in receipts,
        },
        "actions_run_url": _receipt_url(receipts, "actions-run"),
        "pypi_url": _receipt_url(receipts, "pypi"),
    }


def next_step(release: Optional[dict]) -> Optional[str]:
    """The single unambiguous next action for a (re)launched session —
    None exactly when the record is aborted (nothing to advance)."""
    return derive_leg(release)["next_step"]


def format_release_status(release: Optional[dict]) -> str:
    """Human-readable view for `work release status` (read-only; the YAML
    stays authoritative). None — no release recorded yet."""
    if release is None:
        return "No release recorded"
    derived = derive_leg(release)
    if derived["aborted"]:
        head = (f"Release: {derived['version'] or '?'} — aborted "
                f"(terminal; nothing to advance)")
    else:
        head = (f"Release: {derived['version'] or '?'} — {derived['leg']} "
                f"(next: {derived['next_step']})")
    lines = [
        head,
        f"  version               {derived['version'] or '—'}",
        f"  bump-MR merge SHA     {release.get('bump_mr_merge_sha') or '—'}",
        f"  release-MR merge SHA  {derived['merge_sha'] or '—'}",
    ]
    aborted = release.get("aborted")
    if isinstance(aborted, dict):
        reason = aborted.get("reason")
        lines.append(
            f"  aborted               {aborted.get('at') or '?'}"
            + (f" — {reason}" if reason else "")
        )
    tag = release.get("tag")
    if isinstance(tag, dict):
        lines.append(f"  tag                   {tag.get('name')} @ {tag.get('sha')}")
    else:
        lines.append("  tag                   —")
    receipts = release.get("receipts")
    receipts = receipts if isinstance(receipts, dict) else {}
    lines.append("  receipts:")
    for name in RECEIPT_NAMES:
        row = receipts.get(name)
        if isinstance(row, dict):
            detail = row.get("url") or row.get("recorded") or "recorded"
            lines.append(f"    {name:<18} ✓ {detail}")
        else:
            lines.append(f"    {name:<18} —")
    return "\n".join(lines)
