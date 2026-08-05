"""
Write-attribution journal: "who wrote the work repo during a gate window?"
(issue #233).

The test suite's /work leak guard (``tests/conftest.py``) snapshots the
operational work repo's git status and fails the run on any delta. That guard
has two possible causes with opposite meanings — **a test leaked** (issue #93)
versus **the session that launched the suite did its normal job meanwhile**
(``work log``, ``work event``, the writes behind ``work state set`` /
``goal`` / ``artifact`` / ``ledger set``) — and they are indistinguishable by
path: both land in the current run dir. The penalty for the innocent cause was
the whole run.

They ARE distinguishable at the source. The ``work`` CLI is the writer, so at
write time it records the one fact that separates the two causes: whether the
gate-marker holder (the pytest process, for a suite) is among the writer's
process ancestors. A test-spawned ``work`` subprocess descends from pytest;
the session's own writes do not. That is a fact read from the operating system
(``/proc``), not a pattern in anyone's output — the same standard
:mod:`lmer_cli.gate_lock` applies to marker liveness.

Mechanics: while a gate/suite marker is live (:func:`lmer_cli.gate_lock`),
every write-shaped ``work`` invocation appends one JSON line — the journal
lives BESIDE the markers in the lock dir, never in the work repo it describes
— recording the paths it may write and, per live marker, the ancestry verdict
(true / false / null-unknown). At teardown the guard consumes the records
addressed to it (matching work repo, its own pid among the marker pids) and
partitions the drift: non-ancestor writes are the session's and are excused,
ancestor writes are a leak and fail the run naming the command, and anything
unattributed keeps the old hard failure.

Everything fails soft, in both directions that matter: a journal problem never
changes a ``work`` command's exit code, and a write that could not be
journaled (or an ancestry walk that could not complete) leaves its drift
*unattributed* — the guard then behaves exactly as it did before this module
existed. Unknown never becomes a silent pass.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from lmer_cli import gate_lock

#: Journal filename, beside the gate markers in :func:`gate_lock.lock_dir`.
JOURNAL_NAME = "work-writes.jsonl"

#: Journal record schema version.
SCHEMA_VERSION = 1

#: Records older than this are pruned on the next append — the same pid-reuse
#: backstop reasoning as :data:`gate_lock.STALE_AFTER_SECONDS`: a record's
#: guard normally consumes it within one suite run; one that old is orphaned.
STALE_AFTER_SECONDS = gate_lock.STALE_AFTER_SECONDS

#: Walk depth cap for the /proc ancestry climb. A container's process tree is
#: nowhere near this deep; the cap only bounds a pathological /proc.
MAX_CHAIN_DEPTH = 128

#: Size past which an append also compacts the journal. Only a journal whose
#: guards never consume it (suites killed before teardown, forever) grows at
#: all, and records are ~300 bytes — reaching this means thousands of orphaned
#: lines, so the compaction runs effectively never in a healthy container.
COMPACT_AT_BYTES = 1 * 1024 * 1024

_PPID_RE = re.compile(r"^PPid:\s*(\d+)\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Pure logic — unit-testable seams, no filesystem or env reads (gate_lock's
# layout convention).
# ---------------------------------------------------------------------------


def parse_record(text: str) -> Optional[dict]:
    """Normalize one journal line, or None when it is unusable.

    A record is only meaningful if it names the work repo it targeted and at
    least one marker verdict; a torn line or a JSON document of the wrong
    shape is discarded (skipped, never replayed — same tolerance as
    ``run_state.read_events``).
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    work_repo = parsed.get("work_repo")
    markers = parsed.get("markers")
    if not isinstance(work_repo, str) or not work_repo:
        return None
    if not isinstance(markers, list) or not markers:
        return None
    normalized_markers = []
    for entry in markers:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get("pid"))
        except (TypeError, ValueError):
            continue
        ancestor = entry.get("ancestor")
        if ancestor is not None and not isinstance(ancestor, bool):
            ancestor = None
        normalized_markers.append({"pid": pid, "ancestor": ancestor})
    if not normalized_markers:
        return None
    rel_paths = parsed.get("rel_paths")
    if not isinstance(rel_paths, list):
        rel_paths = []
    record = {
        "work_repo": work_repo,
        "markers": normalized_markers,
        "rel_paths": [p for p in rel_paths if isinstance(p, str) and p],
        "command": parsed.get("command") if isinstance(parsed.get("command"), str) else "work",
        "pid": parsed.get("pid"),
    }
    try:
        record["ts"] = float(parsed.get("ts"))
    except (TypeError, ValueError):
        record["ts"] = None
    return record


def marker_verdicts(marker_pids: list[int], chain: Optional[list[int]], chain_complete: bool) -> list[dict]:
    """Per-marker ancestry verdicts from one walked ppid *chain*.

    True is safe to conclude from an incomplete chain (the marker pid was
    seen); False requires the walk to have completed — an aborted walk that
    did not meet the pid is *unknown* (None), which the guard treats exactly
    like an unattributed write. Unknown never becomes a silent pass.
    """
    verdicts = []
    seen = set(chain or [])
    for pid in marker_pids:
        if chain is not None and pid in seen:
            verdicts.append({"pid": pid, "ancestor": True})
        elif chain is not None and chain_complete:
            verdicts.append({"pid": pid, "ancestor": False})
        else:
            verdicts.append({"pid": pid, "ancestor": None})
    return verdicts


def record_addresses_guard(record: dict, work_repo: str, guard_pid: int) -> bool:
    """Whether *record* is addressed to the guard reading it: same (resolved)
    work repo, and the guard's own pid among the marker pids the writer saw."""
    if record.get("work_repo") != work_repo:
        return False
    return any(m.get("pid") == guard_pid for m in record.get("markers", []))


def guard_verdict(record: dict, guard_pid: int) -> Optional[bool]:
    """The record's ancestry verdict for *guard_pid*'s marker (None=unknown).

    Duplicate entries for the same pid resolve worst-verdict-wins
    (True > None > False): the journal sits in a world-readable tmp dir, and a
    crafted record carrying a False beside a True must not flip the guard from
    fail to pass — the excuse is only as strong as the weakest claim in it.
    """
    verdicts = [
        entry.get("ancestor")
        for entry in record.get("markers", [])
        if entry.get("pid") == guard_pid
    ]
    if not verdicts:
        return None
    if any(verdict is True for verdict in verdicts):
        return True
    if any(verdict is None for verdict in verdicts):
        return None
    return False


# ---------------------------------------------------------------------------
# Impure helpers — every one fails open ("no attribution" is the safe answer).
# ---------------------------------------------------------------------------


def journal_path(directory: Optional[Path] = None) -> Path:
    """The journal file, beside the markers (honoring the lock-dir env)."""
    return (directory or gate_lock.lock_dir()) / JOURNAL_NAME


def resolve_work_repo(path: Optional[str] = None) -> str:
    """One canonical spelling for the work repo path, shared by writer and
    guard so their records compare equal (symlinks resolved)."""
    raw = path or os.environ.get("LMER_WORK_REPO_PATH", "/work")
    try:
        return str(Path(raw).resolve())
    except (OSError, RuntimeError):
        return str(raw)


def ppid_chain(pid: Optional[int] = None) -> tuple[Optional[list[int]], bool]:
    """This process's ancestor pids from /proc, self first.

    Returns ``(chain, complete)``: *complete* is True only when the walk
    reached pid 1 (or 0) under the depth cap. A partial chain is still
    returned — finding the marker pid in it is conclusive — but its absence
    from an incomplete chain proves nothing (see :func:`marker_verdicts`).

    A writer whose IMMEDIATE parent is already init reports incomplete even
    though the walk technically finished: ``/proc`` shows *current* parentage,
    so an orphaned process (its spawner double-forked, or exited before this
    write) has had its true ancestry erased by re-parenting. Concluding
    "not a descendant" from an erased chain would let a detached test child be
    excused as the session's — so that residue reads as unknown, which fails
    the suite rather than passing it. Sessions' own `work` invocations run
    under a live harness process tree and are unaffected.
    """
    current = pid if pid is not None else os.getpid()
    chain: list[int] = []
    try:
        for _ in range(MAX_CHAIN_DEPTH):
            chain.append(current)
            if current <= 1:
                break
            text = Path(f"/proc/{current}/status").read_text(encoding="utf-8")
            match = _PPID_RE.search(text)
            if match is None:
                return chain, False
            parent = int(match.group(1))
            if parent == 0:
                break
            current = parent
        else:
            return chain, False
    except (OSError, ValueError, UnicodeDecodeError):
        return (chain or None), False
    orphaned = len(chain) <= 2
    return chain, not orphaned


def record_write_intent(
    command: str,
    rel_paths: list[str],
    work_repo: Optional[str] = None,
) -> None:
    """Journal one write-shaped ``work`` invocation, if a gate is in flight.

    No live marker (or the guard kill switch off) means no guard is armed, so
    nothing is recorded. Every failure is swallowed: a journal problem must
    never change a ``work`` command's exit code, and an unjournaled write only
    restores the pre-#233 behavior (unattributed drift fails the suite).

    A marker held by the CALLING process is included, and since a process is
    trivially its own ancestor the verdict comes out True. That is correct in
    both cases it covers: test code invoking the CLI in-process runs AS the
    pytest process holding the suite marker (a leak, and this is what blames
    it), and ``work verify`` writing receipts under its own marker produces a
    record addressed to a pid no guard ever holds — dead weight for the
    stale-pruner, never a verdict.
    """
    try:
        if not gate_lock.guard_enabled():
            return
        markers = gate_lock.read_markers()
        if not markers:
            return
        chain, complete = ppid_chain()
        record = {
            "schema": SCHEMA_VERSION,
            "ts": time.time(),
            "pid": os.getpid(),
            "command": command,
            "work_repo": resolve_work_repo(work_repo),
            "rel_paths": list(rel_paths),
            "markers": marker_verdicts([m["pid"] for m in markers], chain, complete),
        }
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _append_record(path, record)
    except Exception:
        pass


def _append_record(path: Path, record: dict) -> None:
    """Append *record* as one O_APPEND write.

    Appending (rather than read-modify-write) keeps concurrent session writers
    from truncating each other: kernel-level appends of one small line do not
    interleave destructively, and a torn tail is skipped by parse_record on
    read. Compaction — dropping stale lines — happens on the guard's consume
    rewrite, plus here only past :data:`COMPACT_AT_BYTES` (via tmp+replace, so
    a reader never observes a half-written file; the race of one concurrent
    append lost to the replace is accepted at that size).
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        if path.exists() and path.stat().st_size > COMPACT_AT_BYTES:
            now = time.time()
            kept = [
                text
                for text in path.read_text(encoding="utf-8").splitlines()
                if text.strip()
                and (parsed := parse_record(text)) is not None
                and not (
                    parsed.get("ts") is not None
                    and (now - parsed["ts"]) > STALE_AFTER_SECONDS
                )
            ]
            kept.append(line.rstrip("\n"))
            _replace_journal(path, kept)
            return
    except (OSError, UnicodeDecodeError):
        pass
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _replace_journal(path: Path, lines: list[str]) -> None:
    """Atomically replace the journal with *lines* (tmp file + os.replace).

    In-place truncation is what a reader must never observe: a guard whose
    read raced a writer's truncate would see an empty journal, attribute
    nothing, and fail the suite over the session's own writes — the exact
    symptom this module removes.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    content = "\n".join(lines) + ("\n" if lines else "")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def consume_records(
    work_repo: str,
    guard_pid: int,
    directory: Optional[Path] = None,
) -> list[dict]:
    """The records addressed to this guard; consumed (removed) as read.

    Called by the leak guard at teardown — with an explicit *directory*,
    because the guard's own process runs with the lock dir redirected and must
    name the operational dir it captured before the redirect. Consumption is
    per-GUARD, not per-record: only this guard's marker entry is stripped
    from a matched record, so a record addressed to two overlapping suites
    still carries its verdict for whichever tears down second. Records for
    other guards/repos are rewritten in place (atomically — see
    :func:`_replace_journal`), minus stale ones: this rewrite is the
    journal's routine compaction point, since every suite ends in exactly one
    consume. Fail-open: any problem returns [] and the guard behaves as it
    did before attribution existed.
    """
    path = journal_path(directory)
    resolved = resolve_work_repo(work_repo)
    now = time.time()
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    mine: list[dict] = []
    others: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parsed = parse_record(line)
        if parsed is None:
            continue
        if record_addresses_guard(parsed, resolved, guard_pid):
            mine.append(parsed)
            remaining_markers = [
                entry
                for entry in parsed.get("markers", [])
                if entry.get("pid") != guard_pid
            ]
            if remaining_markers:
                leftover = dict(parsed, markers=remaining_markers)
                leftover["schema"] = SCHEMA_VERSION
                others.append(json.dumps(leftover, ensure_ascii=False))
            continue
        ts = parsed.get("ts")
        if ts is not None and (now - ts) > STALE_AFTER_SECONDS:
            continue
        others.append(line)
    try:
        if not others:
            path.unlink()
        else:
            _replace_journal(path, others)
    except OSError:
        pass
    return mine
