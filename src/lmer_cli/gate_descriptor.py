"""Bounded command description with process ownership isolated from the gate."""

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def describe(argv, *, input, cwd, env, timeout):
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(timeout), *argv],
        input=input, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=timeout + 10, check=True,
    )


def _collect(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)
    deadline = time.monotonic() + 2
    while True:
        children = Path(f"/proc/self/task/{os.getpid()}/children").read_text()
        for child in children.split():
            try:
                os.kill(int(child), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            if time.monotonic() >= deadline:
                raise TimeoutError("Descriptor descendants were not collected")
            time.sleep(0.01)


def main():
    # Only this dedicated process adopts descriptor orphans, never the gate.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot own descriptor descendants")
    request = sys.stdin.read()
    timeout, argv = float(sys.argv[1]), sys.argv[2:]

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(request, timeout=timeout)
        code = process.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode(errors="replace")
        stderr = (exc.stderr or b"").decode(errors="replace") + str(exc) + "\n"
        code = 124
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        _collect(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
