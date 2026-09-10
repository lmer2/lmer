"""Contain a timed connector probe, including its detached descendants."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def run_probe(argv, *, env, timeout, receipt):
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(timeout), str(receipt), *argv],
        env=env, capture_output=True, text=True, check=False,
    )


def main():
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    timeout, receipt = float(sys.argv[1]), Path(sys.argv[2])
    process = subprocess.Popen(sys.argv[3:], start_new_session=True)
    result = {"probe_pid": process.pid, "timed_out": False, "collected": []}
    try:
        try:
            result["returncode"] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            process.kill()
            result["returncode"] = process.wait()
    finally:
        deadline = time.monotonic() + 5
        while True:
            children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
            for pid in map(int, children):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                result["collected"].append({"pid": pid, "returncode": os.waitstatus_to_exitcode(status)})
            else:
                if time.monotonic() >= deadline:
                    raise RuntimeError("probe descendants did not exit after SIGKILL")
                time.sleep(.01)
        result["all_waited"] = True
        receipt.write_text(json.dumps(result))
    if result["timed_out"]:
        return 124
    return result["returncode"] or (1 if result["collected"] else 0)


if __name__ == "__main__":
    sys.exit(main())
