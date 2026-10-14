#!/usr/bin/env python3
"""Manual probe of the detached-container lifecycle (#444) against a real runtime.

Not a test and not a gate: run it on a host with podman and/or docker to confirm
the runtime behaviour lmer relies on (docs/CONTAINER-LIFECYCLE.md):

    python3 scripts/probe_detached_container.py podman docker

Takes about two minutes per runtime. Pulls alpine if needed and removes
everything it creates (label io.lmer.probe). Each check prints PASS/FAIL plus
what it saw; a FAIL names an assumption lmer makes that the runtime does not
meet. It ends with a summary per runtime and exits 0 only if every check on
every runtime passed.
"""

import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time

DETACH_KEYS = "ctrl-],ctrl-\\"
DETACH_BYTES = b"\x1d\x1c"
PROBE = "io.lmer.probe=444"
#: Prints "resized" on SIGWINCH and "STDIN-EOF" if its stdin ever closes (the
#: question for a TUI); exits 7 after ~30 s. The reader holds stdin on fd 3,
#: because a non-interactive shell gives background jobs /dev/null.
WORKLOAD = (
    "trap 'echo resized' WINCH; exec 3<&0; "
    "(cat <&3 >/dev/null; echo STDIN-EOF) & "
    "i=0; while [ $i -lt 120 ]; do sleep 0.25; i=$((i+1)); done; exit 7"
)
#: The current runtime's (name, passed, detail) per check.
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def run(*args, **kwargs):
    return subprocess.run(list(args), capture_output=True, text=True, **kwargs)


def sig_proxy_false(runtime, verb):
    """lmer's rule: --sig-proxy=false wherever the verb takes it."""
    return ["--sig-proxy=false"] if "--sig-proxy" in run(runtime, verb, "--help").stdout else []


def create(runtime, *extra, script=WORKLOAD):
    cid_file = os.path.join(tempfile.mkdtemp(), "cid")
    run(runtime, "create", "-t", "-i", "--init", "--stop-timeout", "200",
        "--label", "io.lmer.managed=1", "--label", PROBE, *extra,
        "--cidfile", cid_file, "alpine", "sh", "-c", script, check=True)
    return open(cid_file).read().strip()


def state(runtime, cid):
    return run(runtime, "inspect", "--format", "{{.State.Status}}", cid).stdout.strip()


class Child:
    """A console client: a Popen, or a raw fork when it must own its terminal.
    Kept whole so it is reaped through its own handle — a dropped Popen is
    reaped behind our back by the next subprocess call."""

    def __init__(self, popen=None, pid=None):
        self.popen, self.pid = popen, pid if popen is None else popen.pid

    def wait(self, timeout):
        """The exit code, or None if still running after *timeout* seconds."""
        if self.popen is not None:
            try:
                return self.popen.wait(timeout)
            except subprocess.TimeoutExpired:
                return None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        return None

    def kill(self):
        try:
            os.kill(self.pid, 9)
        except ProcessLookupError:
            pass


def on_pty(argv, *, controlling, size=(40, 120)):
    """Start *argv* on a fresh PTY of *size* (a 0x0 one is a resize docker
    skips). With *controlling*, as the session leader owning the PTY as its
    terminal, so closing the master sends it SIGHUP. Returns (master, Child)."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
    if controlling:
        pid = os.fork()
        if pid == 0:
            os.close(master)
            os.login_tty(slave)
            os.execvp(argv[0], argv)
        os.close(slave)
        return master, Child(pid=pid)
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True)
    os.close(slave)
    return master, Child(popen=proc)


def drain(master, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                os.read(master, 4096)
            except OSError:
                return


def probe_dead_console(runtime):
    """#444 itself: the console's PTY master dies, then output is provoked."""
    cid = create(runtime)
    master, first = on_pty(
        [runtime, "start", "--attach", "--interactive", *sig_proxy_false(runtime, "start"),
         "--detach-keys", DETACH_KEYS, cid], controlling=False)
    drain(master, 3)
    before = run(runtime, "logs", cid).stdout.count("resized")
    os.close(master)  # the platform daemon dying
    time.sleep(1)
    # Output after that: a second console makes the workload print "resized".
    # A different size, because SIGWINCH comes only from a change; its PTY is
    # held open so its stdin never reaches EOF.
    second, second_child = on_pty(
        [runtime, "attach", *sig_proxy_false(runtime, "attach"),
         "--detach-keys", DETACH_KEYS, cid], controlling=False, size=(41, 121))
    drain(second, 3)
    second_child.kill()
    second_child.wait(5)
    os.close(second)
    time.sleep(2)
    logs = run(runtime, "logs", cid).stdout
    after = logs.count("resized")
    check("output was provoked after the console died", after > before,
          f"'resized' count {before} -> {after}")
    check("container survives a dead console plus output",
          after > before and state(runtime, cid) == "running",
          f"State.Status={state(runtime, cid)!r}, first client exit={first.wait(1)}")
    check("stdin stays open after the console died", "STDIN-EOF" not in logs,
          "a TUI would have seen EOF" if "STDIN-EOF" in logs else "")
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
    replay = run(runtime, "logs", "--since", since, cid)
    check("logs --since accepts lmer's timestamp format and replays TTY output",
          after > 0 and replay.returncode == 0 and replay.stdout.count("resized") >= after,
          f"rc={replay.returncode}, 'resized' replayed {replay.stdout.count('resized')}/{after}"
          f" {replay.stderr.strip()[:100]}")
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    later = run(runtime, "logs", "--since", future, cid)
    check("logs --since a later time replays nothing",
          later.returncode == 0 and "resized" not in later.stdout,
          f"rc={later.returncode} {later.stderr.strip()[:100]}")
    waited = run(runtime, "wait", cid)
    code = (waited.stdout.split() or [""])[-1]
    check("wait returns the real exit code (7)", code == "7", f"got {code!r}")
    check("still present after exit (no --rm)", run(runtime, "inspect", cid).returncode == 0)
    run(runtime, "rm", "--force", cid)
    gone = run(runtime, "wait", cid)
    check("wait on a removed container fails rather than inventing a code",
          gone.returncode != 0, f"rc={gone.returncode} out={gone.stdout.strip()!r}")


def probe_hangup(runtime):
    """Closing a terminal: the client is the session leader on that terminal."""
    cid = create(runtime)
    master, client = on_pty(
        [runtime, "start", "--attach", "--interactive", *sig_proxy_false(runtime, "start"),
         "--detach-keys", DETACH_KEYS, cid], controlling=True)
    drain(master, 3)
    os.close(master)  # the hangup
    status = client.wait(10)
    time.sleep(2)
    check("a terminal hangup on the console leaves the container running",
          state(runtime, cid) == "running",
          f"State.Status={state(runtime, cid)!r}, client exit={status}")
    run(runtime, "rm", "--force", cid)


def probe_detach_keys(runtime):
    """Both verbs lmer attaches with. lmer does not key the detach on the exit
    code (docker's attach is expected nonzero); this records what each gives."""
    for verb in ("start", "attach"):
        cid = create(runtime)
        if verb == "start":
            argv = [runtime, "start", "--attach", "--interactive"]
        else:
            run(runtime, "start", cid, check=True)
            argv = [runtime, "attach"]
        master, client = on_pty(
            [*argv, *sig_proxy_false(runtime, verb), "--detach-keys", DETACH_KEYS, cid],
            controlling=True)
        drain(master, 3)
        os.write(master, DETACH_BYTES)
        drain(master, 1)
        status = client.wait(10)
        check(f"ctrl-],ctrl-\\ detaches `{verb}` and the container keeps running",
              status is not None and state(runtime, cid) == "running",
              f"client exit code={status}")
        os.close(master)
        run(runtime, "kill", cid)
        run(runtime, "rm", "--force", cid)


def probe_stop_timeout(runtime):
    """A plain stop must honour --stop-timeout: the session-end backstop lives
    in that window."""
    cid_file = os.path.join(tempfile.mkdtemp(), "cid")
    run(runtime, "create", "--init", "--stop-timeout", "30", "--label", PROBE,
        "--cidfile", cid_file, "alpine", "sh", "-c",
        "trap 'sleep 15; exit 0' TERM; while true; do sleep 1; done", check=True)
    cid = open(cid_file).read().strip()
    run(runtime, "start", cid, check=True)
    time.sleep(2)
    began = time.monotonic()
    run(runtime, "stop", cid)
    took = time.monotonic() - began
    code = (run(runtime, "wait", cid).stdout.split() or [""])[-1]
    check("plain stop waits for the container's --stop-timeout",
          code == "0" and took >= 12, f"stop took {took:.1f}s, exit code {code!r} "
          "(a 10 s default would give 137)")
    run(runtime, "rm", "--force", cid)


def probe_immediate_removal(runtime):
    """kill, then rm --force, is immediate even with a long stop timeout."""
    cid = create(runtime)
    run(runtime, "start", cid, check=True)
    time.sleep(1)
    began = time.monotonic()
    run(runtime, "kill", cid)
    run(runtime, "rm", "--force", cid)
    took = time.monotonic() - began
    still_there = run(runtime, "inspect", cid).returncode == 0
    check("kill + rm --force removes at once", took < 10 and not still_there,
          f"took {took:.1f}s, still present: {still_there}")


def probe_refused_start(runtime):
    """The GPU/port path: a start the runtime refuses must leave a not-started
    state, which lmer reads as 125 rather than a clean exit."""
    cid_file = os.path.join(tempfile.mkdtemp(), "cid")
    created = run(runtime, "create", "--device", "/dev/lmer-probe-missing", "--label", PROBE,
                  "--cidfile", cid_file, "alpine", "true")
    if created.returncode != 0:
        print("INFO  create itself failed with a missing device, so there is no refused "
              f"start to check: {created.stderr.strip()[:150]}")
        return
    cid = open(cid_file).read().strip()
    started = run(runtime, "start", cid)
    status = state(runtime, cid)
    check("a refused start leaves created/configured",
          started.returncode != 0 and status in ("created", "configured"),
          f"start rc={started.returncode}, State.Status={status!r}")
    run(runtime, "rm", "--force", cid)


def probe_not_started(runtime):
    cid = create(runtime, script="exit 0")
    status = state(runtime, cid)
    check("a never-started container reports created/configured",
          status in ("created", "configured"), f"State.Status={status!r}")
    run(runtime, "rm", "--force", cid)


def probe_label_and(runtime):
    one = create(runtime, "--label", "io.lmer.probe.and=a", script="exit 0")
    both = create(runtime, "--label", "io.lmer.probe.and=a", "--label", "io.lmer.probe.b=1",
                  script="exit 0")
    listed = run(runtime, "ps", "-a", "-q", "--no-trunc", "--filter", "label=io.lmer.probe.and=a",
                 "--filter", "label=io.lmer.probe.b=1").stdout.split()
    check("repeated label filters are ANDed", listed == [both] or
          (len(listed) == 1 and both.startswith(listed[0])), f"listed {listed}")
    for cid in (one, both):
        run(runtime, "rm", "--force", cid)


def probe_names(runtime):
    """What lmer's container names rely on: a taken name fails create, the
    runtime answers whether a name is taken, inspect reports the name, and a
    name filter finds a run's containers by prefix."""
    name = f"lmer-probe-444-{os.urandom(3).hex()}"
    cid = create(runtime, "--name", name, script="exit 0")
    dup_file = os.path.join(tempfile.mkdtemp(), "cid")
    dup = run(runtime, "create", "--name", name, "--label", PROBE, "--cidfile", dup_file,
              "alpine", "true")
    dup_cid = open(dup_file).read().strip() if os.path.exists(dup_file) else ""
    check("a taken name fails create and makes no container",
          dup.returncode != 0 and not dup_cid,
          f"rc={dup.returncode}, cidfile={dup_cid!r}, stderr={dup.stderr.strip()[:150]!r}")
    taken = run(runtime, "inspect", "--type", "container", "--format", "{{.Id}}", name)
    free = run(runtime, "inspect", "--type", "container", "--format", "{{.Id}}", name + "-free")
    check("inspect --type container answers whether a name is taken",
          taken.returncode == 0 and taken.stdout.strip() == cid and free.returncode != 0,
          f"taken rc={taken.returncode}, free rc={free.returncode}")
    record = json.loads(run(runtime, "inspect", cid).stdout)[0]
    check("inspect reports the name", str(record.get("Name", "")).lstrip("/") == name,
          f"Name={record.get('Name')!r}")
    listed = run(runtime, "ps", "-a", "-q", "--no-trunc",
                 "--filter", f"name={name[:-3]}").stdout.split()
    check("ps --filter name=<prefix> finds it", any(cid.startswith(i) for i in listed),
          f"listed {listed}")
    for leftover in (cid, dup_cid):
        if leftover:
            run(runtime, "rm", "--force", leftover)


PROBES = (
    probe_dead_console, probe_hangup, probe_detach_keys, probe_stop_timeout,
    probe_immediate_removal, probe_refused_start, probe_not_started, probe_label_and,
    probe_names,
)


def probe_runtime(runtime):
    """Every probe against one runtime. Returns its (name, passed, detail) list."""
    results.clear()
    print(f"\n=== {runtime} ===")
    if shutil.which(runtime) is None:
        check(f"{runtime} is installed", False, "not found on PATH; nothing was probed")
        return list(results)
    print(f"# {run(runtime, '--version').stdout.strip()}")
    info = run(runtime, "info")
    if info.returncode != 0:
        check(f"{runtime} answers (`{runtime} info`)", False,
              f"{info.stderr.strip()[:200]}; nothing was probed")
        return list(results)
    print(json.dumps({"start sig-proxy": sig_proxy_false(runtime, "start"),
                      "attach sig-proxy": sig_proxy_false(runtime, "attach")}))
    try:
        for probe in PROBES:
            try:
                probe(runtime)
            except subprocess.CalledProcessError as exc:  # report it, go on
                verb = " ".join(exc.cmd[:2])
                check(f"{probe.__name__} ran to completion", False,
                      f"`{verb}` failed ({exc.returncode}): {(exc.stderr or '').strip()[:200]}")
            except Exception as exc:
                check(f"{probe.__name__} ran to completion", False, f"{type(exc).__name__}: {exc}")
    finally:
        leftover = run(runtime, "ps", "-a", "-q", "--filter", f"label={PROBE}").stdout.split()
        if leftover:
            run(runtime, "kill", *leftover)
            run(runtime, "rm", "--force", *leftover)
    return list(results)


def main(runtimes):
    outcome = {runtime: probe_runtime(runtime) for runtime in runtimes}
    print("\n=== summary ===")
    for runtime, checks in outcome.items():
        failed = [name for name, ok, _ in checks if not ok]
        verdict = "ALL PASSED" if not failed else f"{len(failed)} FAILED"
        print(f"{runtime}: {verdict} ({len(checks) - len(failed)}/{len(checks)} passed)")
        for name in failed:
            print(f"  FAIL  {name}")
    return 0 if all(ok for checks in outcome.values() for _, ok, _ in checks) else 1


if __name__ == "__main__":
    runtimes = sys.argv[1:]
    if not runtimes or any(r not in ("podman", "docker") for r in runtimes):
        sys.exit(__doc__)
    sys.exit(main(runtimes))
