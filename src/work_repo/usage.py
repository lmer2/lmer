"""The run's own AI-usage record (#647): ``<run_dir>/ai-usage/<session>.yaml``.

The session writes its record from inside its container, by summarising its
harness transcripts (:mod:`lmer_usage`). Three writers share this module:
- ``work usage --checkpoint``, from the runner's timer: rewrites the file only
  when the counters changed, and never while a gate is running.
- ``work commit``: refreshes the file under the same interval, as part of the
  run-dir commit the run is making anyway.
- ``work session-end``: the final write, ``final: true``. It ignores gates,
  because nothing comes after it to catch up.

Usage must never block delivery: every entry point here logs and returns on
error instead of raising.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from lmer_cli import gate_lock
from lmer_cli.util import get_bool_env
from lmer_usage import buckets, children, record
from lmer_usage.summarise import Summary, summarise

from . import run_state, session_record

ENABLED_ENV = "LMER_USAGE"
CHECKPOINT_ENV = "LMER_USAGE_CHECKPOINT_SECONDS"
DEFAULT_CHECKPOINT_SECONDS = 900
#: Set by spawn-harness on every fan-out child: a child's usage is a row of its
#: parent's record, so the child's own session-end writes no file of its own.
CHILD_ENV = "LMER_USAGE_CHILD"
#: The final write's own budget, inside session-end's 180 s.
FINAL_TIMEOUT_SECONDS = 30
_COMPARED = ("models", "days", "child_days", "totals", "harness_reported_cost", "started_at",
             "ended_at", "active_seconds")


def _warn(message: str) -> None:
    print(f"⚠️  usage: {message}", file=sys.stderr)


def enabled(environ=os.environ) -> bool:
    return get_bool_env(ENABLED_ENV, True, env=environ) and not get_bool_env(CHILD_ENV, False, env=environ)


def checkpoint_seconds(environ=os.environ) -> int:
    raw = (environ.get(CHECKPOINT_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CHECKPOINT_SECONDS
    return value if value > 0 else DEFAULT_CHECKPOINT_SECONDS


def session_dirs() -> list:
    """Every harness's transcript directory in this container."""
    # deferred: import cost — the harness registry, which work's other verbs never need
    from lmer_cli import harness

    dirs = []
    for profile in harness.known_harnesses().values():
        path = getattr(profile, "session_dir", None)
        if path and Path(path) not in dirs:
            dirs.append(Path(path))
    return dirs


def session_files(dirs: Optional[list] = None) -> list:
    """This session's own transcripts; fan-out children's are their own rows."""
    files = []
    for directory in session_dirs() if dirs is None else dirs:
        directory = Path(directory)
        if directory.is_dir() and not directory.is_symlink():
            files.extend(sorted(
                p for p in directory.rglob("*.jsonl")
                if p.is_file() and not p.is_symlink() and not children.is_child_path(p, directory)
            ))
    return files


def child_usage(session: str) -> tuple:
    """This session's fan-out children (#647 Q6) as ``(rows, days)``, from each
    harness's ``.lmer-children`` directory and the container-local fallback.

    Manifests name the session that spawned them, so a container that hosts
    more than one session never counts a child twice.
    """
    roots = [children.children_root(d) for d in session_dirs()]
    roots.append(children.children_root(None))
    return children.read(roots, parent_session=session)


#: Usage the session's own transcripts cannot show (fan-out children, #647 Q6).
#: Each provider takes the session id and returns ``(rows, days)``.
EXTRA_ROW_PROVIDERS: list = [child_usage]


def _extra(session: str) -> tuple:
    rows: list = []
    days: dict = {}
    for provider in EXTRA_ROW_PROVIDERS:
        try:
            found_rows, found_days = provider(session)
        except Exception as exc:
            _warn(f"extra-row provider failed: {exc}")
            continue
        rows.extend(found_rows)
        for day, counters in found_days.items():
            buckets.add(days.setdefault(day, buckets.zero()), counters)
    return rows, days


def _form() -> tuple:
    if not run_state.is_session_form():
        return "legacy", None
    version = session_record.schema_version()
    return ("schema2" if version >= 2 else "schema1"), version


def record_key(state: dict, session: str) -> dict:
    form, schema = _form()
    return {
        "host": os.environ.get("LMER_REPO_HOST"),
        "project": os.environ.get("LMER_REPO_PROJECT"),
        "slug": state.get("slug"),
        "form": form,
        "schema": schema,
        "session": session,
        "uid": session if form == "schema2" else None,
    }


def _linked(target: Optional[str]) -> dict:
    if not isinstance(target, str):
        return {"kind": None, "ref": None}
    for marker, kind in (("/-/issues/", "issue"), ("/issues/", "issue"),
                         ("/-/merge_requests/", "mr"), ("/pull/", "mr")):
        if marker in target:
            project, _, number = target.partition(marker)
            project = project.split("://", 1)[-1].split("/", 1)[-1]
            number = number.split("/", 1)[0].split("#", 1)[0]
            if number.isdigit():
                sep = "#" if kind == "issue" else "!"
                return {"kind": kind, "ref": f"{project}{sep}{number}"}
    return {"kind": None, "ref": None}


def build(state: dict, summary: Summary, *, status: str, final: bool, partial: bool = False,
          now: Optional[datetime] = None, session: Optional[str] = None,
          extra: Optional[tuple] = None) -> dict:
    """``extra`` is ``(rows, days)`` already in hand; by default the providers'."""
    session = session or run_state.current_session_id()
    extra_rows, extra_days = extra if extra is not None else _extra(session)
    try:
        metadata = session_record.provided_metadata() or {}
    except (ValueError, TypeError):
        metadata = {}
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    return record.build(
        key=record_key(state, session),
        summary=summary,
        status=status,
        final=final,
        partial=partial,
        updated_at=now or datetime.now(timezone.utc),
        taskdef=state.get("taskdef") or config.get("taskdef"),
        target=state.get("target"),
        linked=_linked(state.get("target")),
        harness=metadata.get("harness") or os.environ.get("LMER_HARNESS"),
        model=metadata.get("model"),
        effort=metadata.get("effort"),
        platform_session=os.environ.get("LMER_PLATFORM_SESSION_ID") or None,
        extra_rows=extra_rows,
        extra_days=extra_days,
    )


def record_path(rdir: Path, session: str) -> Path:
    return Path(rdir) / record.DIR_NAME / record.file_name(session)


def read_record(path: Path) -> Optional[dict]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _same_counts(old: Optional[dict], new: dict) -> bool:
    return old is not None and all(old.get(name) == new.get(name) for name in _COMPARED)


def _lowers_counts(old: Optional[dict], new: dict) -> bool:
    """True when ``new`` counts less than ``old`` in any counter.

    Transcripts only grow, so a lower count means some went missing (another
    container, a cleared dir): the record keeps what it already proved."""
    old_totals = (old or {}).get("totals")
    if not isinstance(old_totals, dict):
        return False
    new_totals = new.get("totals") or {}
    return any(int(new_totals.get(n, 0)) < int(old_totals.get(n, 0) or 0) for n in buckets.COUNTERS)


def _is_empty(data: dict) -> bool:
    return not data.get("models") and buckets.is_zero(data.get("totals") or {})


def _age_seconds(data: Optional[dict], now: datetime) -> Optional[float]:
    stamp = (data or {}).get("updated_at")
    if not isinstance(stamp, str):
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - then).total_seconds()


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=None), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _record_lock(path: Path):
    """Serialise this session's writers: a checkpoint still running when the
    runner exits must not write over the final record."""
    # Container-local, so it is never staged with the run dir.
    locks = Path(tempfile.gettempdir()) / f"lmer-usage-locks-{os.getuid()}"
    locks.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(locks / f"{path.stem}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _context():
    """(run dir, state, session) when this session has a run record, else None."""
    rdir = run_state.run_dir()
    if rdir is None:
        return None
    try:
        state = run_state.load_state(rdir)
    except run_state.RunStateError:
        return None
    if not state:
        return None
    session = run_state.current_session_id()
    if not record.valid_session(session):
        return None
    return rdir, state, session


def checkpoint(*, respect_interval: bool = False, summarise_fn: Callable = None,
               now: Optional[datetime] = None) -> Optional[Path]:
    """Rewrite this session's record if its counters changed; return its path.

    ``None`` means nothing was written: usage is off, this is a fan-out child,
    there is no run record, a gate is running, the record is already final, the
    counters are unchanged or would go down, there is nothing to record yet, or
    (with ``respect_interval``) the last write is younger than the checkpoint
    interval.
    """
    try:
        if not enabled():
            return None
        if gate_lock.active_gate() is not None:
            return None
        context = _context()
        if context is None:
            return None
        rdir, state, session = context
        now = now or datetime.now(timezone.utc)
        path = record_path(rdir, session)
        existing = read_record(path)
        if existing is not None and existing.get("final") is True:
            return None
        if respect_interval:
            age = _age_seconds(existing, now)
            if age is not None and age < checkpoint_seconds():
                return None
        summary = (summarise_fn or summarise)(session_files())
        data = build(state, summary, status="running", final=False, now=now, session=session)
        with _record_lock(path):
            current = read_record(path)
            if current is not None and current.get("final") is True:
                return None
            if _same_counts(current, data) or (current is None and _is_empty(data)):
                return None
            if _lowers_counts(current, data):
                _warn(f"checkpoint skipped: fewer counts than {path.name} already holds")
                return None
            _write(path, data)
        return path
    except Exception as exc:
        _warn(f"checkpoint failed: {exc}")
        return None


def _collect_within(deadline: float, summarise_fn: Callable, session: str):
    """``(summary, extra)`` for the session and its children, all inside one
    deadline; ``None`` when it runs out."""
    result: dict = {}

    def run():
        try:
            result["summary"] = summarise_fn(session_files())
            result["extra"] = _extra(session)
        except Exception as exc:
            result["error"] = exc

    worker = threading.Thread(target=run, name="usage-final", daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        return None
    if "error" in result:
        raise result["error"]
    return result["summary"], result["extra"]


def _status(exit_code: Optional[int], existing: Optional[dict]) -> str:
    status = "ended" if exit_code in (None, 0) else "crashed"
    # A later non-zero exit turns ended into crashed, never the reverse.
    if existing and existing.get("final") is True and existing.get("status") == "crashed":
        return "crashed"
    return status


def _from_existing(existing: dict) -> tuple:
    """``(summary, extra)`` carrying an earlier record's counts, for a final pass
    that cannot recount. Its children come back as rows with their own
    ``child_days``, and the summary keeps the session's own days."""
    summary = Summary()
    child_rows = [dict(row) for row in existing.get("models") or []
                  if isinstance(row, dict) and row.get("child_session")]
    for row in existing.get("models") or []:
        if not isinstance(row, dict) or row.get("child_session"):
            continue
        if row.get("status", "collected") == "collected":
            key = (row.get("harness"), row.get("model"), row.get("agent"))
            summary.rows[key] = {n: int(row.get(n, 0)) for n in buckets.COUNTERS}
            buckets.add(summary.totals, summary.rows[key])
        else:
            summary.not_collected.append({"reason": row.get("reason") or ""})
    for cost in existing.get("harness_reported_cost") or []:
        if isinstance(cost, dict) and isinstance(cost.get("currency"), str):
            summary.reported_cost[cost["currency"]] = float(cost.get("total") or 0)
    summary.days.update(record.own_days(existing))
    found = existing.get("child_days")
    child_days = {day: {n: buckets.count(c.get(n, 0)) or 0 for n in buckets.COUNTERS}
                  for day, c in (found.items() if isinstance(found, dict) else ())
                  if isinstance(c, dict)}
    summary.active_seconds = int(existing.get("active_seconds") or 0)
    for name in ("started_at", "ended_at"):
        stamp = existing.get(name)
        if isinstance(stamp, str):
            try:
                setattr(summary, name, datetime.fromisoformat(stamp.replace("Z", "+00:00")))
            except ValueError:
                pass
    return summary, (child_rows, child_days)


def finalise(exit_code: Optional[int] = None, *, summarise_fn: Callable = None,
             timeout: float = FINAL_TIMEOUT_SECONDS, now: Optional[datetime] = None) -> Optional[Path]:
    """Write this session's final record (``final: true``), gates notwithstanding."""
    try:
        if not enabled():
            return None
        context = _context()
        if context is None:
            return None
        rdir, state, session = context
        now = now or datetime.now(timezone.utc)
        path = record_path(rdir, session)
        existing = read_record(path)
        status = _status(exit_code, existing)
        collected = _collect_within(timeout, summarise_fn or summarise, session)
        partial = collected is None
        if partial:
            _warn("final pass ran out of time; writing the last checkpoint as partial")
            summary, extra = _from_existing(existing) if existing else (Summary(), ([], {}))
        else:
            summary, extra = collected
        data = build(state, summary, status=status, final=True, partial=partial, now=now,
                     session=session, extra=extra)
        if not partial and _lowers_counts(existing, data):
            _warn(f"final pass counted less than {path.name} already holds; keeping its counts as partial")
            partial = True
            summary, extra = _from_existing(existing)
            data = build(state, summary, status=status, final=True, partial=partial, now=now,
                         session=session, extra=extra)
        with _record_lock(path):
            data["status"] = _status(exit_code, read_record(path))
            _write(path, data)
        return path
    except Exception as exc:
        _warn(f"final write failed: {exc}")
        return None


def rel_path(path: Path) -> Optional[str]:
    """``path`` relative to the work-repo root, for a single-file commit."""
    try:
        return Path(path).resolve().relative_to(run_state.work_repo_root().resolve()).as_posix()
    except (ValueError, OSError):
        return None
