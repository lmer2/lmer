"""Following a detached session container (#444).

``lmer`` creates its session container detached and then *follows* it: it
attaches to the console, waits for the exit, records the real exit code and
removes the container. A dead attach client (a hung-up terminal after a platform
restart, a detach) never touches the container, so the session lives on and
anyone can follow it again — ``lmer follow <container>``, which is what the
platform runs after a restart. ``docs/CONTAINER-LIFECYCLE.md`` has the details.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import signal
import subprocess
import sys
import termios
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .runtime import (
    DETACH_KEYS,
    LABEL_MANAGED,
    RUNTIME_ENV,
    apply_env_file_defaults,
    default_env_file_candidates,
    detect_runtime,
)

__all__ = [
    "create", "follow", "closing_line", "wait_exit_code", "stop", "remove", "main",
    "name_taken", "read_container_id",
]

#: Set by the platform: where to record the container's exit code.
EXIT_FILE_ENV = "LMER_PLATFORM_EXIT_FILE"
#: Set by the platform on the sessions it spawns and follows.
PLATFORM_SESSION_ENV = "LMER_PLATFORM_SESSION_ID"
#: ``State.Status`` of a container that has never run (podman says
#: ``configured`` for one created with no network yet).
NOT_STARTED = ("created", "configured")
_HEX_ID = re.compile(r"[0-9a-f]{12,64}")


def closing_line(code: Optional[int]) -> str:
    shown = "unknown" if code is None else code
    return f"\r\n── lmer: container exited with code {shown} ──\r\n"


def _say(text: str) -> None:
    """lmer's own lines. To stderr unless stdout is a terminal, so a headless
    run's stdout carries only the container's output."""
    stream = sys.stdout if sys.stdout.isatty() else sys.stderr
    # Best-effort: after a platform restart this process's terminal may be gone.
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        pass


def wait_exit_code(runtime: str, container: str) -> Optional[int]:
    """The container's exit code once it stops, or ``None`` if the runtime
    cannot say (the container is gone)."""
    try:
        result = subprocess.run(
            [runtime, "wait", container],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        try:
            sys.stderr.write(result.stderr)
        except (OSError, ValueError):
            pass
        return None
    lines = result.stdout.split()
    try:
        return int(lines[-1])
    except (IndexError, ValueError):
        return None


def state(runtime: str, container: str) -> str:
    """The container's ``State.Status``, or ``""`` when the runtime cannot say."""
    try:
        result = subprocess.run(
            [runtime, "inspect", "--format", "{{.State.Status}}", container],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip().lower() if result.returncode == 0 else ""


def is_running(runtime: str, container: str) -> bool:
    return state(runtime, container) == "running"


def _label(runtime: str, container: str, key: str) -> Optional[str]:
    """The label's value, ``""`` when the container lacks it, ``None`` when the
    container could not be inspected (gone, or a runtime error)."""
    try:
        result = subprocess.run(
            [runtime, "inspect", "--format", f'{{{{index .Config.Labels "{key}"}}}}',
             container],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return "" if value == "<no value>" else value


def _terminal_alive() -> bool:
    """Whether this process's terminal is still there: a hung-up one (its
    master closed) fails ``tcgetattr`` with EIO."""
    try:
        termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, OSError, ValueError):
        return False
    return True


def stop(runtime: str, container: str) -> Optional[subprocess.Popen]:
    """Ask the runtime to stop *container*, without waiting for it."""
    try:
        return subprocess.Popen(
            [runtime, "stop", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def remove(runtime: str, container: str) -> None:
    """Remove *container* now. `kill` first: podman's `rm --force` would honour
    the container's stop timeout and wait minutes; docker's `rm` has no
    `--time`. Killing a container that is not running fails harmlessly."""
    for verb in (["kill"], ["rm", "--force"]):
        subprocess.run(
            [runtime, *verb, container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def read_container_id(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def name_taken(runtime: str, name: str) -> bool:
    """Whether a container already holds *name*. Asked of the runtime rather
    than read off create's error text, whose wording neither runtime promises."""
    try:
        result = subprocess.run(
            [runtime, "inspect", "--type", "container", "--format", "{{.Id}}", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def create(
    runtime: str,
    args: List[str],
    cid_file: Path,
    *,
    rename: Callable[[], str],
    env: Optional[dict] = None,
) -> Tuple[int, str, str]:
    """Run the ``<runtime> create`` command *args*, which names the container
    with ``--name``. Returns the exit code, the new container's id (``""`` if
    none) and the name it got.

    A name already taken is retried once under ``rename()``: stopped containers
    a follower never removed keep their names, and a rerun must not fail on one.
    """
    args = list(args)
    at = args.index("--name") + 1
    for attempt in range(2):
        # stdout is the new container id, which the cidfile already holds.
        rc = subprocess.call(args, env=env, stdout=subprocess.DEVNULL)
        container = read_container_id(cid_file)
        if rc == 0 or container or attempt or not name_taken(runtime, args[at]):
            break
        taken, args[at] = args[at], rename()
        # Both runtimes refuse a cidfile that already exists.
        cid_file.unlink(missing_ok=True)
        _say(f"lmer: container name {taken} is taken; creating {args[at]} instead\n")
    return rc, container, args[at]


def _record_exit_file(container: str, code: int) -> bool:
    """Leave the exit code where the platform can read it if nobody hears this
    process exit (``LMER_PLATFORM_EXIT_FILE``, set by the platform only).
    ``False`` when it could not be written."""
    path = os.environ.get(EXIT_FILE_ENV)
    if not path:
        return True
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"exit_code": code, "container": container}, handle)
        os.replace(tmp, path)
    except OSError as exc:
        _say(f"lmer: cannot record the exit code in {path}: {exc}\n")
        return False
    return True


def _read_exit_file(container: str) -> Optional[int]:
    path = os.environ.get(EXIT_FILE_ENV)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("container") != container:
        return None
    code = record.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _backfill(runtime: str, container: str, since: str) -> None:
    """Replay what the container printed while nobody was attached."""
    try:
        rc = subprocess.call([runtime, "logs", "--since", since, container])
    except OSError as exc:
        rc = f"error: {exc}"
    if rc != 0:
        _say(
            f"\r\n── lmer: output since {since} could not be recovered "
            f"(`{runtime} logs` returned {rc}) ──\r\n"
        )


@functools.lru_cache(maxsize=None)
def _supports_sig_proxy(runtime: str, verb: str) -> bool:
    """Whether ``<runtime> <verb>`` takes ``--sig-proxy``. Asked of the runtime:
    podman's start and attach do, docker's attach does, docker's start does not."""
    try:
        result = subprocess.run(
            [runtime, verb, "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return False
    return "--sig-proxy" in result.stdout


def _console_command(
    runtime: str, container: str, *, started: bool, interactive: bool
) -> List[str]:
    verb = "attach" if started else "start"
    command = [runtime, verb]
    if not started:
        command.append("--attach")
        if interactive:
            command.append("--interactive")
    elif not interactive:
        command.append("--no-stdin")
    # Signals end the console only. A hung-up terminal sends SIGHUP to the
    # attach client, and podman would forward it into the container — ending the
    # session that closing a terminal must no longer end. lmer stops the
    # container itself on SIGTERM.
    if _supports_sig_proxy(runtime, verb):
        command.append("--sig-proxy=false")
    return command + ["--detach-keys", DETACH_KEYS, container]


def follow(
    runtime: str,
    container: str,
    *,
    started: bool,
    interactive: bool,
    since: Optional[str] = None,
    detach_note: Optional[str] = None,
    name: Optional[str] = None,
) -> int:
    """Attach to *container* (starting it first unless *started*), wait for its
    exit, announce and return the code, and remove it.

    SIGTERM stops the container rather than this process: the attach client
    holds nothing up, so ending it alone would leave the session running with
    nobody following it.
    """
    stopping: list = []

    def _stop(signum, frame) -> None:
        if not stopping:
            stopping.append(stop(runtime, container))

    try:
        previous = signal.signal(signal.SIGTERM, _stop)
    except ValueError:
        previous = None
    try:
        if since:
            _backfill(runtime, container, since)
        if not started and stopping:
            # Asked to stop before the container ever ran.
            remove(runtime, container)
            return 143
        command = _console_command(
            runtime, container, started=started, interactive=interactive
        )
        try:
            attach_rc = subprocess.call(command)
        except KeyboardInterrupt:
            _stop(signal.SIGINT, None)
            attach_rc = 130
        except OSError as exc:
            _say(f"lmer: cannot attach to {container}: {exc}\n")
            attach_rc = 125
        if not started and state(runtime, container) in NOT_STARTED:
            # The start itself failed (a GPU or port the runtime could not
            # grant): nothing ran, and `wait` would block on it forever.
            remove(runtime, container)
            return 125
        if (
            not stopping
            and interactive
            and not os.environ.get(PLATFORM_SESSION_ENV)
            and _terminal_alive()
            and is_running(runtime, container)
        ):
            # The console ended, the terminal is still there and the container
            # still runs: hand the terminal back. Not keyed on the console's
            # exit code — a detach exits 0 from `docker start --attach` but
            # nonzero from `docker attach`. A lost terminal, a headless run or a
            # platform follower keeps waiting instead: the code has to reach
            # someone.
            how = "detached" if attach_rc == 0 else f"console ended with code {attach_rc}"
            _say(
                f"\r\n── lmer: {how}; the container keeps running — "
                f"`lmer follow {name or container[:12]}` re-attaches ──\r\n"
            )
            if detach_note:
                _say(f"── {detach_note} ──\r\n")
            return 0
        try:
            code = wait_exit_code(runtime, container)
        except KeyboardInterrupt:
            # Ctrl-C while waiting means stop, as it does on the console.
            _stop(signal.SIGINT, None)
            code = wait_exit_code(runtime, container)
        if code is None:
            # Another follower may have recorded it and removed the container.
            code = _read_exit_file(container)
        _say(closing_line(code))
        if code is None:
            return attach_rc or 1
        # Before the removal, which takes the runtime's copy of the code with it;
        # if it cannot be written, the container stays for the platform to read.
        if _record_exit_file(container, code):
            remove(runtime, container)
        return code
    finally:
        for process in stopping:
            if process is not None:
                process.wait()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def main(argv: List[str]) -> int:
    """``lmer follow <container> [--since TIMESTAMP]``."""
    parser = argparse.ArgumentParser(
        prog="lmer follow",
        description="Attach to a running lmer session container, wait for it to "
        "exit, and remove it.",
    )
    parser.add_argument("container")
    parser.add_argument(
        "--since",
        help="first replay the container's output since this RFC 3339 time",
    )
    args = parser.parse_args(argv)
    # The same runtime choice `lmer` made when it created the container.
    apply_env_file_defaults(default_env_file_candidates(), only=(RUNTIME_ENV,))
    runtime = detect_runtime()
    # Following ends in a removal, so never a container that is provably not
    # lmer's. One that cannot be inspected is followed: `follow` copes with a
    # container that is gone (the exit file may still hold its code).
    managed = _label(runtime, args.container, LABEL_MANAGED)
    if managed is not None and managed != "1":
        print(
            f"lmer follow: {args.container} is not an lmer session container",
            file=sys.stderr,
        )
        return 2
    return follow(
        runtime,
        args.container,
        started=True,
        interactive=sys.stdin.isatty(),
        since=args.since,
        # A name is matched exactly, so the notice must not shorten it like an id.
        name=None if _HEX_ID.fullmatch(args.container) else args.container,
    )
