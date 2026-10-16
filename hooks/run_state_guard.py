#!/usr/bin/env python3
"""
Stop hook: run-state compliance + push-before-stop guard.

The first live ``lmer develop`` test showed an unprimed session skipping all
run-state recording: it did real work on a feature branch while ``phase``,
``goal``, and ``name`` stayed null, leaving a run that was neither resumable
nor findable afterwards. Separately, artifacts were presented to the human
for review while they existed only inside the container — the reviewer works
through the work repo, so an unpushed run dir is invisible to them. Prompt
rules address both, but (as with the Slack reply guard) model self-correction
alone was not enough; this hook is the programmatic backstop for both.

It fires when the agent yields (the Claude Code ``Stop`` event) and carries
TWO independent triggers behind one shared gate:

Trigger 1 — state recording (spec §2). If the workspace shows *real
activity* (feature branch, dirty tree, or commits the upstream/default
branch lacks) while ``work resume --json`` reports a run with ``phase``,
``goal``, or ``name`` null/absent, the stop is blocked with the exact
commands that record the missing pieces. This nudge fires **once per
session**, guarded by a sentinel file — a session that ignores it once is
nagged no further.

Trigger 2 — push before stop (spec §3). Independently, if the run dir in
the work repo carries uncommitted changes or local commits its upstream
lacks, the stop is blocked with a ``work commit`` nudge. Because every stop
that awaits the human must be preceded by a push (compliance is one
always-safe command, and an unpushed artifact defeats the review loop), this
fires on EVERY non-compliant stop — capped at 3 per session via a counter
file, so an environment where pushing genuinely fails (network down) cannot
nag forever. The sentinel (once) and the counter (cap 3) live in separate
``/tmp`` files keyed by ``LMER_SESSION_ID`` so the two cadences never
interfere. When ``work resume --json`` exposes the run dir's web URL
(``run_dir_url``, issue #100 — derived server-side, hooks import no project
code), the block message ends with that clickable deliverable link.

Trigger 3 — ledger after commits (issue #89). If this session landed
project-repo commits (``gate`` events carrying a ``commit_sha``, stamped by
gate-commit) while writing nothing to the run's execution ledger (no
``task`` event from this session), the stop is blocked with a ``work ledger
set`` nudge: unledgered commits are exactly what crash recovery cannot see.
Once per session via its own sentinel, like trigger 1.

All triggers share the kill switch (``LMER_RUN_STATE_GUARD`` unset or
truthy enables; ``=0`` disables — ``get_bool_env`` semantics replicated
inline because hooks import no project code), the run-context gate
(``LMER_REPO_HOST`` + ``LMER_REPO_PROJECT`` set, ``work`` on PATH), and
``stop_hook_active`` handling (no within-yield loops).

They also share one stand-down: **while a gate is in flight** (issue #201),
every trigger is suppressed. A ~14-minute suite makes background gating the
normal pattern, and every nudge here mandates a work-repo write — trigger 2 a
``work commit``, triggers 1 and 3 a ``work goal``/``work state set``/``work
ledger set``. Any of those landing inside the running gate's window moves
tracked files under the suite and fails its ``/work`` isolation guard; the
mandated commit was the session's own stop hook, which is how a gate ended up
arming itself against itself. ``work resume --json`` reports the fact as
``gate_in_flight`` (derived server-side in ``lmer_cli.gate_lock``, the same
pattern as ``run_dir_url`` — hooks import no project code).

The suppression is bounded by the gate's life, never open-ended, which is
what keeps "a session cannot stop with unpushed artifacts" true: deferred work
leaves the run dir dirty, so the moment the gate ends trigger 2 fires normally
and blocks the stop until it lands. No sentinel is burned and no counter is
consumed while suppressed, so a session does not spend its nudges on stops it
was never told about.

Trigger 2 has to find the run dir under BOTH work-repo layouts (issue #353,
spec D7/D20/D22). In a legacy tree it is ``{host}/{project}/runs/{slug}``.
In a schema-form tree — one with a ``_lmer.yaml`` manifest at the work-repo
root — the project is a flat short-name dir named by the manifest's
``projects:`` registry, and the run dir is ``{shortname}/_runs/{slug}``. The
short name is found by normalizing the launch's repo URL (``LMER_REPO_URL``,
else ``https://{host}/{project}``) and looking it up across every entry's
``repo:`` and ``remotes:``; no match means the repo is unnamed and its work
sits in the ``{host}/{project}`` host bucket, which is the legacy path (D9).
The registry lookup and the URL normalization
(``lmer_cli.container.sources.normalize_repo_url``, D21) are replicated
inline here for the same reason as everything else in this file: hooks import
no project code. A manifest that is present but unreadable is NOT demoted to
legacy form (D20 — that would point the guard at a split tree); the hook
simply resolves no run dir and blocks nothing.

The hook fails open everywhere: unreadable payload, git errors, ``work``
failures, JSON parse errors, sentinel/counter I/O errors → exit 0, no
output. Blocking is signalled only via ``{"decision": "block", "reason": …}``
on stdout, never via exit code. The guard only *reads* state — it never
mutates the run, the workspace, or the work repo. A guard that broke the
agent would be worse than the misses it prevents.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

# PyYAML is the one non-stdlib dependency (hooks/start.py takes the same
# one); guarded so a bare interpreter still runs the legacy path — see
# read_plexus_registry for what an absent parser means in schema form.
try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    yaml = None

# Fallback workspace root when the hook payload carries no usable ``cwd``.
DEFAULT_WORKSPACE = "/workspace"

# Work-repo mount point; overridable the same way the work CLI itself is.
DEFAULT_WORK_REPO_PATH = "/work"

# The run-state fields trigger 1 requires, with the exact command that
# records each — the block reason lists precisely the missing subset.
STATE_FIELDS = ("phase", "goal", "name")
STATE_COMMANDS = {
    "phase": "work state set --phase=<name>",
    "goal": 'work goal "<one-line goal>"',
    "name": "work name <kebab-case>",
}

# Trigger 2 fires on every non-compliant stop, but never more than this many
# times per session (a broken push must not nag forever).
PUSH_NUDGE_CAP = 3

# Subprocess budgets. A Stop hook runs on every yield; a hung `work` or git
# call must not stall the session, so both are bounded and any timeout fails
# open.
WORK_TIMEOUT_SECONDS = 5
GIT_TIMEOUT_SECONDS = 5

# Session-scoped marker files. Separate files keep the once-per-session
# cadence of trigger 1 and the capped-repeat cadence of trigger 2 from
# interfering with each other.
SENTINEL_TEMPLATE = "/tmp/lmer_run_state_guard.{session}"
COUNTER_TEMPLATE = "/tmp/lmer_run_state_guard_push.{session}"
LEDGER_SENTINEL_TEMPLATE = "/tmp/lmer_run_state_guard_ledger.{session}"

# get_bool_env semantics (src/lmer_cli/util.py), replicated inline: hooks are
# standalone-stdlib and import no project code.
_TRUTHY = {"1", "yes", "true"}
_FALSY = {"0", "no", "false"}

# Schema-form work repo (spec §4): the manifest at the work-repo root whose
# ``projects:`` registry names the flat project dirs, and the per-project
# runs dir beneath a short name (D22). Legacy trees have neither.
PLEXUS_MANIFEST = "_lmer.yaml"
SCHEMA_RUNS_DIR = "_runs"
LEGACY_RUNS_DIR = "runs"

# normalize_repo_url (src/lmer_cli/container/sources.py), replicated inline
# — the same rules, so a registry value and the launch URL compare apples to
# apples (D21). Default ports fold away; any other port stays in the key.
_DEFAULT_PORTS = ("443", "22")
_SCHEME_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://")

# Spec §1 short-name charset. A registry key becomes a path segment under
# the work root, and the work repo is agent-writable — anything outside this
# set is skipped, never joined (the same check taskdefs._plexus_project_dir
# applies; ``os.path.join`` would reset on an absolute key).
_SHORT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


# ---------------------------------------------------------------------------
# Pure logic — unit-testable seams. No env reads, no filesystem, no
# subprocesses; every input is injected by the caller.
# ---------------------------------------------------------------------------


def env_flag(value: str | None, default: bool = True) -> bool:
    """
    Parse a boolean env-var *value* with ``get_bool_env`` semantics.

    Truthy ``1/yes/true``, falsy ``0/no/false`` (case-insensitive); unset,
    empty, or unrecognized values return ``default``. The guard's kill switch
    is "unset or truthy enables", so its default is True.
    """
    text = (value or "").strip().lower()
    if not text:
        return default
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def parse_resume_json(stdout: str) -> dict | None:
    """
    Extract the resume decision object from ``work resume --json`` stdout.

    The command prints a single JSON line today, but tolerate stray leading
    output by falling back to the last JSON-parsable line. Returns ``None``
    when no object can be recovered (e.g. the "No run context" message) —
    the caller fails open.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        decision = json.loads(text)
        return decision if isinstance(decision, dict) else None
    except (ValueError, TypeError):
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            decision = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(decision, dict):
            return decision
    return None


def missing_state_fields(decision: dict | None) -> list[str]:
    """
    Trigger-1 compliance decision, given the resume JSON.

    Returns the subset of ``phase``/``goal``/``name`` that is null or absent,
    in canonical order — empty when compliant. Anything that is not a
    ``kind == "run"`` decision is treated as compliant (nothing to record on
    a fresh/absent run; ``work resume`` itself seeds state elsewhere).
    """
    if not isinstance(decision, dict) or decision.get("kind") != "run":
        return []
    return [field for field in STATE_FIELDS if decision.get(field) is None]


def detect_activity(
    current_branch: str | None,
    default_branch: str | None,
    status_porcelain: str | None,
    ahead_count: int | None,
) -> bool:
    """
    Trigger-1 activity decision, given raw git outputs for the workspace.

    Real activity is any of: the current branch differs from the repo's
    default branch; the working tree is dirty (any porcelain output); or the
    branch has commits its upstream (or the default-branch tip) lacks. A
    fresh session parked on a clean default branch shows none of these and
    must not be nudged — it has done nothing worth recording yet.
    """
    if current_branch and default_branch and current_branch != default_branch:
        return True
    if status_porcelain is not None and status_porcelain.strip():
        return True
    if ahead_count is not None and ahead_count > 0:
        return True
    return False


def run_dir_noncompliance(
    status_porcelain: str | None,
    ahead_count: int | None,
) -> tuple[bool, bool]:
    """
    Trigger-2 decision, given raw git outputs scoped to the run dir.

    Returns ``(dirty, unpushed)``: dirty when the scoped ``git status
    --porcelain`` shows anything; unpushed when commits touching the run dir
    exist that the upstream lacks. ``None`` inputs (git/upstream state
    unreadable) count as compliant — fail open, never block on uncertainty.
    """
    dirty = bool(status_porcelain is not None and status_porcelain.strip())
    unpushed = bool(ahead_count is not None and ahead_count > 0)
    return dirty, unpushed


def _userinfo_boundary(text: str, scp_form: bool = False) -> int:
    """Index of the ``@`` separating userinfo from host, or ``-1`` if none.

    Replica of sources.py's helper: the boundary is the last ``@`` before the
    first path separator, with the one extension that a later ``@`` preceded
    by ``user:secret`` shape (a non-numeric right-hand side) is taken instead,
    so a credential carrying ``/`` is still seen. For scp-form the text after
    the boundary must itself carry a ``:`` (``[user@]host:path``), which keeps
    a bare ``host:org/re@po.git`` from reading as userinfo.
    """
    slash = text.find("/")
    at = text.rfind("@", 0, slash if slash != -1 else len(text))
    if at == -1 and "@" in text:
        candidate = text.rpartition("@")[0]
        _, colon, secret = candidate.partition("/")[0].partition(":")
        if colon and not secret.isdigit():
            if not scp_form or ":" in text[len(candidate) + 1:]:
                at = len(candidate)
    return at


def _parse_repo_url(url: str | None) -> tuple[str, str, str, str, str]:
    """Split a git URL into ``(scheme, userinfo, host, port, path)``.

    Replica of sources.py's parser: scheme URLs and scp-form both handled,
    never raises. scp-form is git's ``[user@]host:path`` — no scheme and a
    ``:`` before any ``/``, the user optional — minus the two shapes the
    canonical parser keeps out of it: bare digits after the colon are a
    port (``host:2222/path`` is the normalizer's own ported spelling and
    stays opaque), and the ambiguous ``a:b/c@d`` shape stays opaque too.
    Anything else scheme-less comes back entirely in ``path``.
    """
    url = (url or "").strip()
    m = _SCHEME_RE.match(url)
    if m:
        rest = url[m.end():]
        at = _userinfo_boundary(rest)
        userinfo, rest = (rest[:at], rest[at + 1:]) if at != -1 else ("", rest)
        netloc, _, path = rest.partition("/")
        host, _, port = netloc.partition(":")
        return m.group("scheme").lower(), userinfo, host, port, path
    at = _userinfo_boundary(url, scp_form=True)
    if at != -1:
        host, _, path = url[at + 1:].partition(":")
        return "", url[:at], host, "", path
    host, colon, path = url.partition(":")
    if (
        colon
        and host
        and "/" not in host
        and not path.partition("/")[0].isdigit()
        and _userinfo_boundary(url) == -1
    ):
        return "", "", host, "", path
    return "", "", "", "", url


def normalize_repo_url(url: str | None) -> str:
    """Canonical comparison form of a git repo URL: ``host/path``.

    Replica of ``lmer_cli.container.sources.normalize_repo_url`` (D21):
    lowercase the host, drop scheme and userinfo, drop the default ports
    443/22 but keep any other as ``host:port``, fold scp (user optional) and
    ``ssh://`` forms into ``host/path``, strip a trailing ``.git`` and
    trailing ``/``. Path
    case is preserved here and compared case-insensitively by the lookup.
    Pinned against the original by tests/test_run_state_guard_second_name.py.
    """
    _, _, host, port, path = _parse_repo_url(url)
    host = host.lower()
    if port and port not in _DEFAULT_PORTS:
        host = f"{host}:{port}"
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    if host and path:
        return f"{host}/{path}"
    return host or path


def registry_lookup(registry: dict | None, repo_url: str | None) -> str | None:
    """Short name of the registry entry whose ``repo:`` or any ``remotes:``
    value names ``repo_url`` (spec §4 resolution, D8); None when nothing does.

    Both sides pass through ``normalize_repo_url`` and compare
    case-insensitively on the path. Keys that are not §1 short names
    (``_SHORT_NAME_RE``), entries that are not mappings, repoless entries
    (D19), and non-string remote values are simply skipped — the validator
    owns shape complaints, the hook only needs an answer it can safely join
    into a path.
    """
    if not isinstance(registry, dict):
        return None
    wanted = normalize_repo_url(repo_url).lower()
    if not wanted:
        return None
    for name, entry in registry.items():
        if not isinstance(name, str) or not _SHORT_NAME_RE.fullmatch(name):
            continue
        if not isinstance(entry, dict):
            continue
        candidates = [entry.get("repo")]
        remotes = entry.get("remotes")
        if isinstance(remotes, list):
            candidates.extend(remotes)
        for value in candidates:
            if isinstance(value, str) and normalize_repo_url(value).lower() == wanted:
                return name
    return None


def derive_run_dir(
    decision: dict | None,
    host: str | None,
    project: str | None,
    work_repo_path: str | None = None,
    repo_url: str | None = None,
) -> str | None:
    """
    Resolve the run-dir path trigger 2 inspects.

    Preference order: an explicit path exposed by the resume JSON (future-
    proofing — the decision does not carry one today); the conventional
    runs dir for the work repo's form plus ``/{slug}`` when the decision
    names a slug; otherwise the project's whole runs dir, so unpushed run
    artifacts are still caught even when the slug is unknown. Returns
    ``None`` without run context.

    The conventional dir is ``{work}/{host}/{project}/runs`` in a legacy
    tree and ``{work}/{shortname}/_runs`` in a schema-form one (D20/D22),
    where the short name comes from looking ``repo_url`` (``LMER_REPO_URL``,
    else ``https://{host}/{project}``) up in the manifest's registry. No
    registry match is the host bucket — the legacy path (D9). The one
    filesystem touch here is that manifest read, via ``read_plexus_registry``:
    a manifest that is present but unusable resolves to ``None`` rather than
    to the legacy path (D20), and so blocks nothing.
    """
    if isinstance(decision, dict):
        exposed = decision.get("run_dir")
        if isinstance(exposed, str) and exposed.strip():
            return exposed
    if not host or not project:
        return None
    work = work_repo_path or DEFAULT_WORK_REPO_PATH
    try:
        registry = read_plexus_registry(work)
    except Exception:
        return None  # present-but-unusable manifest: no base to resolve
    base = os.path.join(work, host, project, LEGACY_RUNS_DIR)
    if registry is not None:
        lookup_url = repo_url.strip() if isinstance(repo_url, str) and repo_url.strip() \
            else f"https://{host}/{project}"
        short_name = registry_lookup(registry, lookup_url)
        if short_name:
            base = os.path.join(work, short_name, SCHEMA_RUNS_DIR)
    slug = decision.get("slug") if isinstance(decision, dict) else None
    if isinstance(slug, str) and slug.strip():
        return os.path.join(base, slug)
    return base


def derive_run_dir_url(decision: dict | None) -> str | None:
    """The run dir's web URL, when ``work resume --json`` exposed one.

    ``run_dir_url`` (issue #100) is derived server-side by the work CLI —
    hooks import no project code — with credentials already stripped there.
    Returns None when the field is absent/blank; the trigger-2 nudge then
    falls back to the path-only message exactly as before.
    """
    if not isinstance(decision, dict):
        return None
    url = decision.get("run_dir_url")
    if isinstance(url, str) and url.strip():
        return url
    return None


def derive_gate_in_flight(decision: dict | None) -> bool:
    """Whether a gate command is running right now (issue #201).

    Read from ``work resume --json``'s ``gate_in_flight``, which the work CLI
    derives from the gate markers — the hook never inspects the marker
    directory itself, for the same reason it never derives the run-dir URL:
    one definition, on the side that owns it. Absent (an older work CLI, an
    idle machine) reads as False, which is exactly the pre-#201 behavior.
    """
    if not isinstance(decision, dict):
        return False
    return bool(decision.get("gate_in_flight"))


def ledger_nudge_needed(events: list | None, session: str) -> bool:
    """
    Trigger-3 decision, given the run's parsed events and the session id.

    True when this session landed at least one project-repo commit (a
    ``gate`` event whose data carries a ``commit_sha`` — gate-commit stamps
    one exactly when a commit succeeded) while recording no ``task`` event,
    i.e. nothing was written to the execution ledger. Events from other
    sessions never count either way: a prior session's ledger writes do not
    excuse this session's unledgered commits, and its commits are not this
    session's to ledger. ``None`` events (log unreadable/absent) count as
    compliant — fail open.
    """
    if not events:
        return False
    committed = False
    for event in events:
        if not isinstance(event, dict) or event.get("session") != session:
            continue
        if event.get("type") == "task":
            return False
        data = event.get("data")
        if event.get("type") == "gate" and isinstance(data, dict) and data.get("commit_sha"):
            committed = True
    return committed


def build_ledger_reason() -> str:
    """Trigger-3 block reason: the `work ledger set` nudge."""
    return (
        "Ledger check: this session committed to the project repo but wrote "
        "nothing to the run's execution ledger — unledgered commits are "
        "exactly what crash recovery cannot see. Record each landed task "
        "now — `work ledger set <task-id> --status done --commit <sha>` — "
        "then stop again. (This nudge fires once per session; if the "
        "commits genuinely map to no plan task, stopping again proceeds "
        "normally.)"
    )


def build_state_reason(missing: list[str]) -> str:
    """Trigger-1 block reason: exactly the missing pieces, exact commands."""
    pieces = ", ".join(missing)
    commands = "; ".join(f"`{STATE_COMMANDS[field]}`" for field in missing)
    return (
        "Run-state check: this session shows real work in the workspace, but "
        f"the run record is missing: {pieces}. An unrecorded run cannot be "
        "resumed or found later. Record the missing pieces now — "
        f"{commands} — then stop again. "
        "(This nudge fires once per session; if the work genuinely does not "
        "belong to a run, stopping again proceeds normally.)"
    )


def build_push_reason(
    dirty: bool,
    unpushed: bool,
    run_dir: str | None = None,
    run_dir_url: str | None = None,
) -> str:
    """Trigger-2 block reason: the `work commit` nudge.

    When ``work resume --json`` exposed the run dir's web URL (issue #100 —
    the URL is derived server-side because hooks import no project code),
    the nudge ends with the clickable link the pushed deliverable will live
    at, instead of leaving only a container path.
    """
    problems = []
    if dirty:
        problems.append("uncommitted changes")
    if unpushed:
        problems.append("local commits its upstream lacks")
    where = f" ({run_dir})" if run_dir else ""
    reason = (
        f"Push-before-stop check: the run dir{where} has "
        f"{' and '.join(problems)}. The reviewer works through the work repo "
        "— an unpushed artifact is invisible to the reviewer. Run `work commit` "
        "now, "
        "then stop again. (Capped at 3 nudges per session in case pushing "
        "genuinely cannot succeed.)"
    )
    if run_dir_url:
        reason += f"\nOnce pushed, the deliverable is linkable at: {run_dir_url}"
    return reason


def evaluate(
    *,
    missing_fields: list[str],
    activity: bool,
    state_already_nudged: bool,
    run_dir_dirty: bool,
    run_dir_unpushed: bool,
    push_nudge_count: int,
    run_dir: str | None = None,
    run_dir_url: str | None = None,
    push_cap: int = PUSH_NUDGE_CAP,
    ledger_needed: bool = False,
    ledger_already_nudged: bool = True,
    gate_in_flight: bool = False,
) -> dict:
    """
    Combine the triggers into one decision, from fully injected inputs.

    Returns ``{"state_reason": str | None, "ledger_reason": str | None,
    "push_reason": str | None}`` — one entry per trigger so the caller can
    drop a reason whose bookkeeping side effect (sentinel / counter write)
    fails, keeping fail-open per trigger rather than all-or-nothing.

    Trigger 1 fires when there is real activity, at least one missing state
    field, and the session has not been nudged before. Trigger 3 fires when
    the injected ledger check found unledgered commits and its own sentinel
    is unset (defaults keep it off for callers that never gathered it).
    Trigger 2 fires independently whenever the run dir is dirty or unpushed
    and the session cap has not been reached.

    ``gate_in_flight`` overrides all three (issue #201): every nudge here
    mandates a work-repo write, and one landing inside a running gate's window
    fails that gate. Returning empty reasons — rather than a softer message —
    also leaves the sentinels and the counter untouched, so nothing is spent on
    a stop the session was never told about, and the nudges return in full the
    moment the gate ends.
    """
    if gate_in_flight:
        return {"state_reason": None, "ledger_reason": None, "push_reason": None}

    state_reason = None
    if missing_fields and activity and not state_already_nudged:
        state_reason = build_state_reason(missing_fields)

    ledger_reason = None
    if ledger_needed and not ledger_already_nudged:
        ledger_reason = build_ledger_reason()

    push_reason = None
    if (run_dir_dirty or run_dir_unpushed) and push_nudge_count < push_cap:
        push_reason = build_push_reason(
            run_dir_dirty, run_dir_unpushed, run_dir, run_dir_url
        )

    return {
        "state_reason": state_reason,
        "ledger_reason": ledger_reason,
        "push_reason": push_reason,
    }


# ---------------------------------------------------------------------------
# Impure gatherers — every one fails open by returning None on any error.
# ---------------------------------------------------------------------------


def _git(cwd: str, *args: str) -> str | None:
    """Run a git command; return stripped stdout, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _default_branch(root: str) -> str:
    """The repo's default branch: ``origin/HEAD``, falling back to ``main``."""
    ref = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return ref.rsplit("/", 1)[-1]
    return "main"


def _workspace_ahead_count(root: str, default_branch: str) -> int | None:
    """
    Commits the current branch has that its upstream lacks; with no upstream
    set, commits the default-branch tip lacks. None when nothing resolves.
    """
    for base in ("@{upstream}", f"origin/{default_branch}", default_branch):
        count = _git(root, "rev-list", "--count", f"{base}..HEAD")
        if count is not None:
            try:
                return int(count)
            except ValueError:
                return None
    return None


def gather_workspace_activity(root: str) -> bool:
    """Collect git facts for the workspace and run the pure activity check."""
    current_branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if current_branch is None:
        return False  # not a git repo / git broken — fail open
    default_branch = _default_branch(root)
    status = _git(root, "status", "--porcelain")
    ahead = _workspace_ahead_count(root, default_branch)
    return detect_activity(current_branch, default_branch, status, ahead)


def gather_run_dir_status(run_dir: str) -> tuple[bool, bool]:
    """
    Collect git facts scoped to the run dir and run the pure trigger-2 check.

    Both commands run from inside the run dir with a ``-- .`` pathspec, so a
    busy work repo cannot trip the guard on other projects' changes.
    """
    if not os.path.isdir(run_dir):
        return (False, False)
    status = _git(run_dir, "status", "--porcelain", "--", ".")
    ahead_text = _git(run_dir, "rev-list", "--count", "@{upstream}..HEAD", "--", ".")
    ahead: int | None
    try:
        ahead = int(ahead_text) if ahead_text is not None else None
    except ValueError:
        ahead = None
    return run_dir_noncompliance(status, ahead)


def read_run_events(run_dir: str) -> list | None:
    """
    Parse the run dir's events.jsonl; None when absent or unreadable.

    Torn/corrupt lines are skipped (a crash mid-append must not break the
    guard) — same tolerance as the work CLI's own reader, replicated here
    because hooks import no project code.
    """
    path = os.path.join(run_dir, "events.jsonl")
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def read_plexus_registry(work_repo_path: str) -> dict | None:
    """
    The ``projects:`` registry of the work repo's ``_lmer.yaml``.

    ``None`` when there is no manifest (legacy tree); a dict — possibly empty,
    when the manifest declares no projects — when there is one. A manifest
    that is present but cannot be used (unreadable, not YAML, not a mapping,
    a ``projects:`` that is not a mapping, or no YAML parser on this
    interpreter) raises ``ValueError``: the caller must NOT read that as a
    legacy tree (D20), and the guard's answer is to resolve nothing.
    """
    path = os.path.join(work_repo_path, PLEXUS_MANIFEST)
    if not os.path.exists(path):
        return None
    if yaml is None:
        raise ValueError("PyYAML unavailable; cannot read the plexus manifest")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh.read())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"unreadable plexus manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("plexus manifest is not a mapping")
    registry = data.get("projects")
    if registry is None:
        return {}
    if not isinstance(registry, dict):
        raise ValueError("plexus manifest 'projects' is not a mapping")
    return registry


def _run_work_resume() -> dict | None:
    """``work resume --json`` (read-only), parsed; None on any failure."""
    try:
        result = subprocess.run(
            ["work", "resume", "--json"],
            capture_output=True,
            text=True,
            timeout=WORK_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return parse_resume_json(result.stdout)


def _read_push_count(path: str) -> int | None:
    """Nudge count from the counter file: 0 when absent, None when unreadable."""
    try:
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or "0")
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    """
    Stop-hook entrypoint. Reads the hook payload from stdin and, inside a
    run context, blocks the stop when the run record is incomplete (once per
    session) or the run dir is unpushed (every stop, capped at 3/session).

    Always returns 0: blocking is signalled via the JSON ``decision`` field
    on stdout, never via exit code, and every failure path falls open.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return 0

    # Already continuing from a previous nudge — do not block again (no loops).
    if payload.get("stop_hook_active"):
        return 0

    # Kill switch: unset or truthy enables; LMER_RUN_STATE_GUARD=0 disables.
    if not env_flag(os.environ.get("LMER_RUN_STATE_GUARD")):
        return 0

    # Gate: run context plus the work CLI. Without either there is no run
    # state to check and no safe way to check it.
    host = os.environ.get("LMER_REPO_HOST", "").strip()
    project = os.environ.get("LMER_REPO_PROJECT", "").strip()
    if not host or not project:
        return 0
    if shutil.which("work") is None:
        return 0

    decision = _run_work_resume()
    if decision is None:
        return 0

    session = os.environ.get("LMER_SESSION_ID", "").strip() or "unknown"
    sentinel_path = SENTINEL_TEMPLATE.format(session=session)
    counter_path = COUNTER_TEMPLATE.format(session=session)

    # Issue #201: with a gate in flight every trigger stands down, so the
    # gathering below is skipped too — those are git subprocesses run on every
    # yield, and their answers cannot change the verdict.
    gate_in_flight = derive_gate_in_flight(decision)

    # Trigger 1 inputs. Activity needs git subprocesses, so gather it only
    # when the cheaper checks (missing fields, sentinel) leave it in play.
    missing = missing_state_fields(decision)
    try:
        state_already_nudged = os.path.exists(sentinel_path)
    except OSError:
        state_already_nudged = True  # sentinel unreadable — fail open, no nudge
    activity = False
    if missing and not state_already_nudged and not gate_in_flight:
        cwd = payload.get("cwd")
        root = cwd if isinstance(cwd, str) and cwd.strip() else DEFAULT_WORKSPACE
        try:
            activity = gather_workspace_activity(root)
        except Exception:
            activity = False

    # Trigger 2 inputs.
    work_repo_path = os.environ.get("LMER_WORK_REPO_PATH", "").strip() or DEFAULT_WORK_REPO_PATH
    repo_url = os.environ.get("LMER_REPO_URL", "").strip() or None
    run_dir = derive_run_dir(decision, host, project, work_repo_path, repo_url)
    run_dir_url = derive_run_dir_url(decision)
    dirty, unpushed = (False, False)
    if run_dir and not gate_in_flight:
        try:
            dirty, unpushed = gather_run_dir_status(run_dir)
        except Exception:
            dirty, unpushed = (False, False)
    push_count = _read_push_count(counter_path)
    if push_count is None:
        push_count = PUSH_NUDGE_CAP  # counter unreadable — fail open, no nudge

    # Trigger 3 inputs. The events log is read only when the sentinel leaves
    # the trigger in play (and only from the resolved run dir).
    ledger_sentinel_path = LEDGER_SENTINEL_TEMPLATE.format(session=session)
    try:
        ledger_already_nudged = os.path.exists(ledger_sentinel_path)
    except OSError:
        ledger_already_nudged = True  # sentinel unreadable — fail open, no nudge
    ledger_needed = False
    if run_dir and not ledger_already_nudged and not gate_in_flight:
        try:
            ledger_needed = ledger_nudge_needed(read_run_events(run_dir), session)
        except Exception:
            ledger_needed = False

    verdict = evaluate(
        missing_fields=missing,
        activity=activity,
        state_already_nudged=state_already_nudged,
        run_dir_dirty=dirty,
        run_dir_unpushed=unpushed,
        push_nudge_count=push_count,
        run_dir=run_dir,
        run_dir_url=run_dir_url,
        ledger_needed=ledger_needed,
        ledger_already_nudged=ledger_already_nudged,
        gate_in_flight=gate_in_flight,
    )

    # Bookkeeping BEFORE blocking, each failing open per trigger: a nudge
    # whose sentinel/counter cannot be recorded could repeat unboundedly, so
    # it is dropped instead.
    reasons: list[str] = []
    if verdict["state_reason"]:
        try:
            with open(sentinel_path, "w", encoding="utf-8"):
                pass
            reasons.append(verdict["state_reason"])
        except OSError:
            pass
    if verdict["ledger_reason"]:
        try:
            with open(ledger_sentinel_path, "w", encoding="utf-8"):
                pass
            reasons.append(verdict["ledger_reason"])
        except OSError:
            pass
    if verdict["push_reason"]:
        try:
            with open(counter_path, "w", encoding="utf-8") as fh:
                fh.write(str(push_count + 1))
            reasons.append(verdict["push_reason"])
        except OSError:
            pass

    if reasons:
        json.dump({"decision": "block", "reason": "\n\n".join(reasons)}, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
