"""``lmer platform`` — run and inspect the orchestrator daemon.

Subcommands
-----------
``run`` (default)
    Serve the control plane with uvicorn on the configured address and port, start
    the orchestrating assistant before it does (spec §8.1, T63) and tick detection
    for as long as it serves (spec §8.3, T69) — the one subcommand that launches a
    container and the one that runs background threads, deliberately: a diagnostic
    verb that did either as a side effect would be a verb nobody could run while
    working out what is wrong.
``status``
    Print configuration, mirror staleness and run counts without starting a
    server. The first thing to reach for when the fleet view looks wrong.
``secret``
    Print the shared secret, creating one on first use — how an operator gets the
    credential into a browser or a ``curl``.
``rescan``
    Force a work-repo pull and print the resulting counts, offline.
``setup-ui``
    Fetch a pinned Node into the platform's state dir and build the control UI
    with it (spec D10) — the host needs no Node of its own.
``runs`` / ``adopt`` / ``forget``
    Manage the tracked-run index that scopes the fleet view (spec D25). ``runs``
    lists what this orchestrator tracks; ``runs --candidates`` lists everything in
    the shared work repo so an operator can pick something to ``adopt``.
``spawn`` / ``resume``
    Start a session. ``spawn`` starts a new run; ``resume`` continues one this
    orchestrator already tracks, defaulting to the taskdef that run recorded
    (:mod:`lmer_platform.resume`). Both are the CLI's half of the routes the UI
    uses, so an operator with a terminal never has to reach for ``curl`` and a
    token to do something the browser can.

``status`` and ``rescan`` exist because a daemon that only runs as a server is
hard to diagnose: when the inventory looks wrong the question is almost always
"is the mirror stale" or "did the pull fail", and both answers should be one
command away without curl or a token.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from .api import build_state, create_app
from .assistant import Supervisor, register_supervisor
from .config import (
    ConfigError, binding_notice, ensure_secret, load as load_config, read_secret,
)
from .detect import Detector
from .reattach import reattach_all, startup_notice
from .resume import ResumeError, ResumeRequest, resume_run
from .runs import forget, list_tracked, run_key, track
from .spawn import CapacityError, RunAlreadyLive, SpawnError, SpawnRequest, spawn_session
from .store import StoreError, platform_dir
from .ui_build import (
    NODE_VERSION, UIBuildError, installed_ui_dir, is_built, node_dir, platform_key,
    setup_ui,
)
from .workrepo import mirror_status, pull, resolve_run_dir, run_dirs

logger = logging.getLogger("lmer_platform.daemon")

__all__ = ["main", "build_arg_parser"]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lmer platform",
        description="Run and inspect the lmer orchestrator platform.",
    )
    sub = parser.add_subparsers(dest="command")

    def add_bind_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--bind", dest="bind_address",
            help="Host address to bind (default: config.json, else 127.0.0.1)",
        )
        target.add_argument(
            "--port", dest="bind_port", type=int,
            help="Port to bind (default: config.json, else 8600)",
        )

    run = sub.add_parser("run", help="Serve the control plane (default)")
    add_bind_flags(run)
    run.add_argument(
        "--log-level", default="info",
        help="uvicorn log level (default: info)",
    )

    status = sub.add_parser("status", help="Print config, mirror state and counts")
    add_bind_flags(status)
    status.add_argument("--json", action="store_true", help="Emit JSON")

    sub.add_parser("secret", help="Print the shared secret, creating it if absent")
    rescan = sub.add_parser("rescan", help="Force a work-repo pull, then report")
    rescan.add_argument("--json", action="store_true", help="Emit JSON")

    runs_cmd = sub.add_parser("runs", help="List the runs this orchestrator tracks")
    runs_cmd.add_argument(
        "--candidates", action="store_true",
        help="Instead list every run in the shared work repo (adoption picker)",
    )

    adopt = sub.add_parser("adopt", help="Track an existing run: <host>/<project>/<slug>")
    adopt.add_argument(
        "run", help="e.g. gitlab.example.com/group/project/develop-issue-141"
    )
    adopt.add_argument("--note", help="Free-form note recorded with the entry")

    forget_cmd = sub.add_parser("forget", help="Stop tracking a run")
    forget_cmd.add_argument("run", help="<host>/<project>/<slug>")

    setup_ui_cmd = sub.add_parser(
        "setup-ui", help="Fetch a pinned Node and build the control UI"
    )
    setup_ui_cmd.add_argument(
        "--force-node", action="store_true",
        help="Re-download and re-extract Node even if it is already present",
    )

    spawn_cmd = sub.add_parser("spawn", help="Start a session and track its run")
    spawn_cmd.add_argument("taskdef", help="Task definition, e.g. develop")
    spawn_cmd.add_argument("target", help="Task target (issue/MR URL, branch, ...)")
    spawn_cmd.add_argument("--repo", dest="repo_url", help="Repository URL")
    spawn_cmd.add_argument("--preset", help="lmer preset name")
    # The fan-out roster (issue #130), and the same flag POST /api/sessions takes.
    # A typed field on the request rather than something appended to extra_args:
    # the platform emits `--agents` itself and records what it emitted, so a second
    # spelling in argv would beat the registry entry instead of colliding with it
    # (spawn._RESERVED_ARGS refuses exactly that).
    spawn_cmd.add_argument(
        "--agents",
        help="Comma-delimited preset names the session may fan a task out to; "
             "resolved against this host's presets file",
    )
    spawn_cmd.add_argument("--harness", help="Harness name")
    spawn_cmd.add_argument(
        "--ports", type=int, default=0,
        help="Number of host ports to publish for the session",
    )

    resume_cmd = sub.add_parser(
        "resume", help="Continue a tracked run by starting its next session"
    )
    resume_cmd.add_argument("run", help="<host>/<project>/<slug>")
    resume_cmd.add_argument(
        "--taskdef",
        help="Run this taskdef against the run's target instead of the one it "
             "recorded — that starts a SIBLING run against the same target "
             "(develop -> review), not this one",
    )
    resume_cmd.add_argument(
        "--repo", dest="repo_url",
        help="Repository URL for a run that has none recorded, or to correct a "
             "wrong one; the platform will not invent it",
    )
    # Spelled --prompt because that is where the value goes: `lmer --prompt`, which
    # the container reads as LMER_START_PROMPT and records as the run's seed. The
    # API field is `direction`, and the help text says both words so an operator
    # who knows either finds it.
    resume_cmd.add_argument(
        "--prompt", dest="direction",
        help="What the next session should do — the run's seed direction. Required "
             "to reopen a run that is already complete or archived",
    )

    return parser


def _split_run_ref(raw: str) -> tuple:
    """Parse ``<host>/<project>/<slug>`` where *project* may contain slashes.

    Project paths are not a fixed number of segments (``group/project`` versus
    ``group/subgroup/project``), so the split is first-segment host,
    last-segment slug, everything between is the project.
    """
    parts = [p for p in (raw or "").strip().strip("/").split("/") if p]
    if len(parts) < 3:
        raise ConfigError(
            f"expected <host>/<project>/<slug>, got {raw!r} — the project may "
            "contain slashes, but host and slug are one segment each"
        )
    return parts[0], "/".join(parts[1:-1]), parts[-1]


def _overrides(args: argparse.Namespace) -> dict:
    return {
        "bind_address": getattr(args, "bind_address", None),
        "bind_port": getattr(args, "bind_port", None),
    }


def _cmd_secret(args: argparse.Namespace) -> int:
    config = load_config()
    token = ensure_secret(config)
    print(token)
    print(f"# stored in {config.secret_path} (mode 0600)", file=sys.stderr)
    return 0


def _print_status(payload: dict, as_json: bool) -> None:
    if as_json:
        import json

        print(json.dumps(payload, indent=2))
        return

    config = payload["config"]
    mirror = payload["mirror"]
    totals = payload["totals"]

    print(f"platform state : {platform_dir()}")
    print(f"bind           : {config['base_url']}")
    print(f"work repo      : {config['work_repo_url'] or '(not configured)'}")
    print(f"mirror         : {'present' if mirror['present'] else 'absent'}"
          f"{'' if mirror['healthy'] else '  ⚠️  STALE'}")
    print(f"last pull      : {mirror['last_pull_at'] or 'never'}")
    if mirror.get("last_error"):
        print(f"last error     : {mirror['last_error']}")
    print(f"runs           : {totals['runs']} "
          f"({totals['live']} live, {totals['attention']} need you)")
    counts = payload.get("counts") or {}
    if counts:
        breakdown = ", ".join(f"{state}={n}" for state, n in sorted(counts.items()))
        print(f"by state       : {breakdown}")
    for run in payload.get("attention", []):
        attention = run["attention"]
        print(f"  ⚠️  {run['label']} [{attention['reason']}] {attention['note'] or ''}")


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(_overrides(args))
    _print_status(build_state(config), args.json)
    return 0


def _cmd_rescan(args: argparse.Namespace) -> int:
    config = load_config()
    status = pull(config, force=True)
    if not status.healthy:
        print(f"⚠️  mirror not healthy: {status.last_error}", file=sys.stderr)
    _print_status(build_state(config), args.json)
    return 0 if status.healthy else 1


def _supervise_assistant(config) -> Supervisor:
    """Start the assistant, say what happened, and keep it running (spec §8.1).

    Called from :func:`_cmd_run` and from no other subcommand: the assistant is a
    container, and ``status``/``rescan``/``runs``/``adopt``/``forget``/``spawn``/
    ``setup-ui`` are things an operator runs *while* diagnosing, often several
    times in a row.

    Never fatal, and that is the whole shape of this function: a platform that
    refuses to boot because the assistant would not start is worse than one that
    boots and reports the assistant down, because the fleet view is what an
    operator needs when something is broken and it does not depend on the
    assistant at all. :meth:`lmer_platform.assistant.Supervisor.first_start`
    absorbs every failure into a report; this prints it and serves either way.

    The supervisor is *published* as well as started (T75): a give-up ends its
    loop, and ``POST /api/assistant/start`` — the route the give-up tells the
    operator to call — has to be able to reach it, or the assistant it brings back
    is one nothing watches. See
    :func:`lmer_platform.assistant.register_supervisor` for why that is
    process-wide state rather than an argument to the app.
    """
    supervisor = Supervisor(config)
    print(supervisor.first_start().notice, flush=True)
    supervisor.start()
    register_supervisor(supervisor)
    return supervisor


def _watch_for_attention(config) -> Detector:
    """Start the detection tick that notifies the assistant (spec §8.3, T69).

    Called from :func:`_cmd_run` and from no other subcommand, the same rule the
    assistant auto-start follows: ``status``, ``rescan``, ``runs``, ``adopt``,
    ``forget``, ``spawn`` and ``setup-ui`` are things an operator runs *while*
    diagnosing, often several times over, and a diagnostic verb that grew a
    background thread would start pulling a work repo and writing digests behind
    the answer they asked for.

    In a thread rather than inline, unlike
    :meth:`lmer_platform.assistant.Supervisor.first_start`: the first tick reads
    the fleet view, which may clone or fetch a mirror, and there is nothing in it
    for an operator to read — it is the baseline (see :mod:`lmer_platform.detect`)
    and notifies nobody. Making the bind wait on a ``git`` operation would delay
    the one thing that must come up.
    """
    detector = Detector(config)
    print(detector.notice, flush=True)
    detector.start()
    return detector


def _cmd_run(args: argparse.Namespace) -> int:
    import uvicorn

    config = load_config(_overrides(args))
    secret = ensure_secret(config)

    # flush=True throughout: stdout is a pipe under nohup/systemd, so Python
    # block-buffers it while uvicorn logs to stderr unbuffered. Without the
    # flush the operator sees uvicorn's "running on ..." but not the bind notice
    # or how to authenticate — exactly the two things they need at startup.
    print(binding_notice(config), flush=True)
    if read_secret(config):
        print(
            "🔑 Authenticate with the shared secret: "
            "`Authorization: Bearer <secret>`, or let your browser prompt you "
            "(any username). Print it with `lmer platform secret`.",
            flush=True,
        )
    status = mirror_status(config)
    if not status.present:
        print(
            "📁 Work-repo mirror not cloned yet — it is created on the first "
            "state read. Set work_repo_url (or LMER_WORK_REPO) if the fleet "
            "view comes back empty.",
            flush=True,
        )
    if not is_built():
        print(
            "🖼  Control UI not built — serving the JSON API only. Build it with "
            "`lmer platform setup-ui`, run once from your lmer checkout (only the "
            "build needs the sources; the result is installed to "
            f"{installed_ui_dir()} and read from there afterwards).",
            flush=True,
        )

    # Before the server accepts anything. Sessions outlive the daemon (spec R11)
    # but their host PTYs do not — the master fd died with whatever process ran
    # last, and with it the thread that fed each session's log. Re-attaching here
    # means the first request for a survivor's scrollback already sees the seam
    # and the recovered output, rather than a file that stopped growing (T36).
    notice = startup_notice(reattach_all())
    if notice:
        print(notice, flush=True)

    # After the re-attach and before the server, in that order for both halves:
    # a surviving assistant has to be re-attached first or this would see it as
    # unmarked and the ensure would race the marking, and an assistant that is up
    # before uvicorn accepts anything is one whose row is in the first fleet view
    # the UI renders.
    supervisor = _supervise_assistant(config)
    # After the assistant, before the server: a digest spools whether or not one
    # is running (:func:`lmer_platform.assistant.notify`), so this order is not
    # about correctness — it is that the assistant's own row is in the fleet view
    # the first tick baselines against, so its start is never itself an event.
    detector = _watch_for_attention(config)

    app = create_app(config, secret)
    uvicorn.run(
        app,
        host=config.bind_address,
        port=config.bind_port,
        log_level=args.log_level,
        access_log=False,
    )
    # The server has stopped serving. A supervisor sitting in its backoff would
    # otherwise start a container with nothing left to serve it — and the
    # assistant is deliberately *not* stopped here: sessions outlive the daemon
    # (spec R11), and ending one is ``assistant.stop``'s bookkeeping to do when an
    # operator asks for it.
    supervisor.stop()
    # And withdrawn, in that order: a stopped supervisor left reachable would
    # answer a re-arm for a daemon that is shutting down (it refuses, but the
    # refusal is a detail of ``resume``), and in a process that runs this more than
    # once — tests, an embedding caller — it would answer for the previous run.
    register_supervisor(None)
    # Nothing is left to read a digest the way nothing is left to serve, and a
    # detector sitting in its interval would otherwise keep fetching a mirror for
    # a daemon that has stopped.
    detector.stop()
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    config = load_config()
    if args.candidates:
        tracked_keys = {(e.host, e.project, e.slug) for e in list_tracked()}
        found = run_dirs(config)
        if not found:
            print("no runs found in the work-repo mirror "
                  "(is work_repo_url set, and has it pulled?)")
            return 0
        print(f"{len(found)} run(s) in the shared work repo "
              "— these are everyone's, not the fleet view:")
        for ref in found:
            mark = "tracked" if (ref.host, ref.project, ref.slug) in tracked_keys else "-"
            print(f"  [{mark:>7}] {ref.rel_path}")
        return 0

    tracked = list_tracked()
    if not tracked:
        print(
            "No runs tracked. The fleet view is scoped to runs this "
            "orchestrator spawned or adopted — never the whole shared work "
            "repo, which other devs also use.\n"
            "  see candidates : lmer platform runs --candidates\n"
            "  adopt one      : lmer platform adopt <host>/<project>/<slug>"
        )
        return 0

    # Keys, not paths: the listing above this one names *directories* it found in
    # the mirror, and this one names what the index holds — which is identity, and
    # for a named run is not the directory that identity lives in (runs.TrackedRun).
    print(f"{len(tracked)} tracked run(s):")
    for entry in tracked:
        print(f"  [{entry.source:>8}] {entry.key}"
              f"{'  taskdef=' + entry.taskdef if entry.taskdef else ''}")
    return 0


def _cmd_adopt(args: argparse.Namespace) -> int:
    host, project, slug = _split_run_ref(args.run)
    config = load_config()
    entry = track(host, project, slug, source="adopted", note=args.note)

    if resolve_run_dir(config, host, project, slug) is None:
        print(
            f"⚠️  adopted {entry.key}, but no such run dir is in the mirror "
            "yet — it may not be pushed, or the mirror may be stale "
            "(`lmer platform rescan`).",
            file=sys.stderr,
        )
    print(f"✅ tracking {entry.key}")
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    host, project, slug = _split_run_ref(args.run)
    # Echoed as the key the operator typed. These two lines used to compose
    # ``<host>/<project>/runs/<slug>``, which for a named run is a directory that
    # does not exist — and this verb touches no directory at all.
    key = run_key(host, project, slug)
    if forget(host, project, slug):
        print(f"✅ no longer tracking {key}")
        return 0
    print(f"not tracked: {key}", file=sys.stderr)
    return 1


def _cmd_spawn(args: argparse.Namespace) -> int:
    config = load_config()
    request = SpawnRequest(
        taskdef=args.taskdef,
        target=args.target,
        repo_url=args.repo_url,
        preset=args.preset,
        agents=args.agents,
        harness=args.harness,
        ports=args.ports,
    )
    try:
        result = spawn_session(config, request)
    except CapacityError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    # Same exit as capacity, not SpawnError's 2: the request is fine, the run is
    # just held right now — retrying after the holder ends may work.
    except RunAlreadyLive as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except SpawnError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    print(f"✅ spawned {result.session_id} (pid {result.pid})")
    # The identity is the host and the project. The slug is derived from the
    # taskdef and the target, so it is *always* set (``run_state.derive_slug``
    # answers for any taskdef) — testing it here printed
    # `run  : None/None/runs/develop-…` for a run that was never tracked and left
    # the branch below unreachable, so a log line was the only sign that the row
    # would vanish when the session exited.
    if result.host and result.project:
        print(f"   run  : {result.host}/{result.project}/runs/{result.slug}")
    else:
        print("   run  : identity unknown — this run is not tracked")
        if result.warning:
            # Worded for the person who typed the target, and the same text the
            # spawn API returns; see spawn._untracked_run_warning.
            print(f"   why  : {result.warning}")
    print(f"   log  : {result.log_path}")
    return 0


#: Which flag supplies each refusal that is a *request for one more field*, keyed
#: on :attr:`lmer_platform.resume.ResumeError.code`. The refusals are worded for
#: the HTTP API — "Supply repo_url with the resume" — which is not something a
#: shell user can type, and the code exists precisely so a client can translate
#: without matching on English. Rewriting the message per caller would be the
#: alternative, and then the two would drift.
_RESUME_REMEDIES = {
    "repo_url_required": "   supply it     : --repo <url>",
    "direction_required": "   say what to do: --prompt '<what the session should do>'",
}


def _cmd_resume(args: argparse.Namespace) -> int:
    """Continue a tracked run from a terminal — the CLI half of the resume route.

    Exit codes follow ``spawn``'s: 1 is the concurrency cap (the same request may
    work later), 2 is anything the caller has to change. The direction is never
    echoed, printed or logged here, for the reason :mod:`lmer_platform.resume`
    gives — it is the operator's content, and it is already in their shell history
    if they want it.
    """
    host, project, slug = _split_run_ref(args.run)
    config = load_config()
    request = ResumeRequest(
        host=host,
        project=project,
        slug=slug,
        taskdef=args.taskdef,
        repo_url=args.repo_url,
        direction=args.direction,
    )
    try:
        result = resume_run(config, request)
    except ResumeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        remedy = _RESUME_REMEDIES.get(exc.code)
        if remedy:
            print(remedy, file=sys.stderr)
        return 2
    # Before SpawnError, which CapacityError subclasses.
    except CapacityError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    # Held-run refusals are the capacity case, not the bad-request case.
    except RunAlreadyLive as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except SpawnError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    payload = result.to_dict()
    started = payload["started"]
    # "resumed" and "started" are different events and the reply already knows
    # which one happened: an overridden taskdef derives a different run dir, so the
    # session lands on a sibling and the run the operator named is untouched.
    print(f"✅ {'resumed' if result.continued else 'started'} "
          f"{started['host']}/{started['project']}/runs/{started['slug']}")
    print(f"   session : {result.session.session_id} (pid {result.session.pid})")
    print(f"   taskdef : {result.taskdef}")
    print(f"   note    : {payload['note']}")
    return 0


def _cmd_setup_ui(args: argparse.Namespace) -> int:
    print(f"⬇️  Fetching pinned Node {NODE_VERSION} for {platform_key()} "
          f"into {node_dir()} (nothing is installed system-wide)", flush=True)
    try:
        dist = setup_ui(force_node=args.force_node)
    except UIBuildError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(f"✅ UI built into {dist}")
    print("   `lmer platform run` now serves it at the configured bind address.")
    return 0


_COMMANDS = {
    "run": _cmd_run,
    "setup-ui": _cmd_setup_ui,
    "status": _cmd_status,
    "secret": _cmd_secret,
    "rescan": _cmd_rescan,
    "runs": _cmd_runs,
    "adopt": _cmd_adopt,
    "forget": _cmd_forget,
    "spawn": _cmd_spawn,
    "resume": _cmd_resume,
}


def _load_env_files() -> None:
    """Seed the environment from ``.env`` files before resolving anything.

    ``lmer platform`` is dispatched before ``lmer``'s own argument parsing, so it
    would otherwise never load ``~/.lmer/.env`` — and that is exactly where an
    operator keeps ``LMER_WORK_REPO`` and its token. Without this the daemon
    starts with no work repo configured and the fleet view is empty for a reason
    that looks like a bug.

    First-wins, so an exported variable still beats the file, and spawned sessions
    inherit the result (they also load ``.env`` themselves, harmlessly).
    """
    from lmer_cli.cli import apply_env_file_defaults, default_env_file_candidates

    loaded = apply_env_file_defaults(default_env_file_candidates())
    if loaded:
        logger.debug("platform_env_files_loaded vars=%d", len(loaded))


def main(argv: Optional[list] = None) -> int:
    _load_env_files()
    parser = build_arg_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    command = args.command or "run"
    if command == "run" and not hasattr(args, "log_level"):
        # `lmer platform` with no subcommand: fill in run's defaults.
        args = parser.parse_args(["run"])
        command = "run"

    try:
        return _COMMANDS[command](args)
    except (ConfigError, StoreError) as exc:
        print(f"lmer platform: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
