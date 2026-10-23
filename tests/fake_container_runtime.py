"""A stand-in for ``docker``/``podman`` covering the verbs lmer's detached
session containers use (#444): create, start --attach, attach, wait, inspect,
ps, stop, rm, logs. Containers are named and found by id or name; a taken name
fails create as both runtimes do (exit 125), in the wording of
``FAKE_RUNTIME_FLAVOR``.

State lives in ``$FAKE_RUNTIME_STATE``. A "container" is a detached process
whose output goes to a file, so it outlives any client exactly as a real
detached container does. Set ``FAKE_RUNTIME_NO_LOGS=1`` to make ``logs`` fail.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

IMAGE = "fake-image"
STATE = Path(os.environ["FAKE_RUNTIME_STATE"])
VALUED = {
    "--cidfile", "--label", "--detach-keys", "--env-file", "-e", "--user", "-w",
    "--security-opt", "--since", "--filter", "--stop-timeout", "--format", "--name",
    "--type",
}
#: `--help` text, modelled on docker (its attach proxies signals, its start has
#: no such flag) or, with FAKE_RUNTIME_FLAVOR=podman, on podman (both do).
HELP = {"start": "--attach --interactive --detach-keys", "attach": "--no-stdin --sig-proxy --detach-keys"}
if os.environ.get("FAKE_RUNTIME_FLAVOR") == "podman":
    HELP["start"] += " --sig-proxy"


def box(ref):
    """A container's directory, by id or by name."""
    if (STATE / ref).exists():
        return STATE / ref
    for path in STATE.iterdir() if STATE.exists() else []:
        try:
            if json.loads((path / "config.json").read_text()).get("name") == ref:
                return path
        except (OSError, ValueError):
            continue  # being created or removed
    return STATE / ref


def publish(path, text):
    """Write *path* whole or not at all: every reader polls for these files
    and reads them the moment they exist, so a half-written one reads as ""."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text)
    os.replace(partial, path)


def _conflict(name, cid):
    if os.environ.get("FAKE_RUNTIME_FLAVOR") == "podman":
        return (
            f'Error: creating container storage: the container name "{name}" is already '
            f"in use by {cid}. You have to remove that container to be able to reuse "
            "that name: that name is already in use"
        )
    return (
        f'docker: Error response from daemon: Conflict. The container name "/{name}" is '
        f'already in use by container "{cid}". You have to remove (or rename) that '
        "container to be able to reuse that name."
    )


def options(args):
    """(flags, values, positionals) with values grouped by option."""
    values, positionals, index = {}, [], 0
    while index < len(args):
        arg = args[index]
        if arg in VALUED:
            values.setdefault(arg, []).append(args[index + 1])
            index += 2
        elif arg.startswith("-"):
            values.setdefault(arg, []).append(True)
            index += 1
        else:
            positionals.append(arg)
            index += 1
    return values, positionals


def running(cid):
    return (box(cid) / "pid").exists() and not (box(cid) / "exit").exists()


def stream(cid, offset):
    """Copy the container's output from *offset* until it has exited and all is read."""
    out = box(cid) / "out.log"
    while True:
        done = (box(cid) / "exit").exists()
        with out.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
        if chunk:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            offset += len(chunk)
        elif done:
            return int((box(cid) / "exit").read_text())
        else:
            time.sleep(0.05)


def create(args):
    image_at = args.index(IMAGE)
    if "--detach-keys" in args[:image_at]:
        # As docker's create does: detach keys belong to start and attach.
        print("unknown flag: --detach-keys", file=sys.stderr)
        return 125
    values, _ = options(args[:image_at])
    cid_file = Path(values["--cidfile"][0])
    if cid_file.exists():
        print(f"container ID file found, make sure the other container isn't "
              f"running or delete {cid_file}", file=sys.stderr)
        return 125
    cid = uuid.uuid4().hex
    name = values.get("--name", [f"fake_{cid[:8]}"])[0]
    if box(name).exists():
        print(_conflict(name, box(name).name), file=sys.stderr)
        return 125
    box(cid).mkdir(parents=True)
    labels = dict(label.split("=", 1) for label in values.get("--label", []))
    publish(box(cid) / "config.json", json.dumps({
        "labels": labels, "command": args[image_at + 1:], "name": name,
        "stop_timeout": int(values.get("--stop-timeout", [10])[0]),
    }))
    (box(cid) / "out.log").write_bytes(b"")
    publish(cid_file, cid)
    return 0


def start(args):
    _, positionals = options(args)
    cid = positionals[-1]
    if os.environ.get("FAKE_RUNTIME_FAIL_START"):
        # As a runtime refusing a device or a port: nothing runs.
        print("Error: error gathering device information", file=sys.stderr)
        return 125
    subprocess.Popen(
        [sys.executable, __file__, "_run", cid],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    while not (box(cid) / "pid").exists():
        time.sleep(0.01)
    return stream(cid, 0) if "--attach" in args else 0


def attach(args):
    _, positionals = options(args)
    cid = positionals[-1]
    return stream(cid, (box(cid) / "out.log").stat().st_size)


def run_container(cid):
    config = json.loads((box(cid) / "config.json").read_text())
    with (box(cid) / "out.log").open("ab", buffering=0) as out:
        process = subprocess.Popen(config["command"], stdout=out, stderr=out)
        publish(box(cid) / "pid", str(process.pid))
        code = process.wait()
    code = 128 - code if code < 0 else code
    publish(box(cid) / "finished", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    publish(box(cid) / "exit", str(code))
    return 0


def wait(args):
    cid = args[-1]
    if not box(cid).exists():
        print(f"Error: no such container: {cid}", file=sys.stderr)
        return 1
    while not (box(cid) / "exit").exists():
        time.sleep(0.05)
    print(int((box(cid) / "exit").read_text()))
    return 0


def inspect(args):
    if os.environ.get("FAKE_RUNTIME_FAIL_INSPECT"):
        print("Error: runtime unavailable", file=sys.stderr)
        return 1
    if args[:2] == ["--type", "container"]:
        args = args[2:]
    if args[0] == "--format":
        # Only the templates lmer uses.
        cid = args[-1]
        if not box(cid).exists():
            return 1
        exited = (box(cid) / "exit").exists()
        status = "running" if running(cid) else "exited" if exited else "created"
        if "Labels" in args[1]:
            key = args[1].split('"')[1]
            labels = json.loads((box(cid) / "config.json").read_text())["labels"]
            print(labels.get(key, "<no value>"))
        elif "Status" in args[1]:
            print(status)
        elif args[1] == "{{.Id}}":
            print(box(cid).name)
        else:
            print(str(running(cid)).lower())
        return 0
    records = []
    for cid in args:
        if not box(cid).exists():
            print(f"Error: no such object: {cid}", file=sys.stderr)
            return 1
        config = json.loads((box(cid) / "config.json").read_text())
        exited = (box(cid) / "exit").exists()
        records.append({
            "Id": box(cid).name,
            # docker's spelling; podman has no leading slash.
            "Name": ("" if os.environ.get("FAKE_RUNTIME_FLAVOR") == "podman" else "/")
            + config.get("name", ""),
            "Config": {"Labels": config["labels"]},
            "State": {
                "Status": "running" if running(cid) else "exited" if exited else "created",
                "Running": running(cid),
                "ExitCode": int((box(cid) / "exit").read_text()) if exited else 0,
                "FinishedAt": (box(cid) / "finished").read_text() if exited else None,
            },
        })
    print(json.dumps(records))
    return 0


def ps(args):
    values, _ = options(args)
    filters = values.get("--filter", [])
    wanted = [f.split("=", 1)[1] for f in filters if f.startswith("label=")]
    ids = [f.split("=", 1)[1] for f in filters if f.startswith("id=")]
    for path in sorted(STATE.iterdir()) if STATE.exists() else []:
        if ids and path.name not in ids:
            continue
        try:
            labels = json.loads((path / "config.json").read_text())["labels"]
        except OSError:
            # Being created or removed: a real runtime's listing never fails
            # because a container came or went during it (#589).
            continue
        pairs = {f"{k}={v}" for k, v in labels.items()}
        if all(want in pairs for want in wanted):
            print(path.name)
    return 0


def stop(args):
    """TERM, then KILL once the container's stop timeout runs out."""
    cid = args[-1]
    if not running(cid):
        return 0
    pid = int((box(cid) / "pid").read_text())
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + json.loads((box(cid) / "config.json").read_text())["stop_timeout"]
    while running(cid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if running(cid):
        os.kill(pid, signal.SIGKILL)
    # A follower may record the exit and remove the container first.
    while box(cid).exists() and not (box(cid) / "exit").exists():
        time.sleep(0.05)
    return 0


def kill(args):
    cid = args[-1]
    if not running(cid):
        print(f"Error: container {cid} is not running", file=sys.stderr)
        return 1
    os.kill(int((box(cid) / "pid").read_text()), signal.SIGKILL)
    return 0


def rm(args):
    cid = args[-1]
    if not box(cid).exists():
        return 0
    was_running = running(cid)
    # Gone at once, as a real removal is: a concurrent `wait` must not read an
    # exit code the kill below produces.
    gone = STATE.parent / f"removed-{cid}"
    os.rename(box(cid), gone)
    if was_running:
        os.kill(int((gone / "pid").read_text()), signal.SIGKILL)
    shutil.rmtree(gone, ignore_errors=True)
    return 0


def logs(args):
    if os.environ.get("FAKE_RUNTIME_NO_LOGS"):
        print("Error: configured logging driver does not support reading", file=sys.stderr)
        return 1
    out = box(args[-1]) / "out.log"
    values, _ = options(args)
    since = values.get("--since", [None])[0]
    # Coarse but honest: nothing written since a time after the last write.
    if since and time.strptime(since, "%Y-%m-%dT%H:%M:%SZ") > time.gmtime(out.stat().st_mtime):
        return 0
    sys.stdout.buffer.write(out.read_bytes())
    return 0


VERBS = {
    "create": create, "start": start, "attach": attach, "_run": lambda a: run_container(a[0]),
    "wait": wait, "inspect": inspect, "ps": ps, "stop": stop, "kill": kill, "rm": rm,
    "logs": logs,
}

if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True)
    if os.environ.get("FAKE_RUNTIME_LOG"):
        with open(os.environ["FAKE_RUNTIME_LOG"], "a") as log:
            log.write(json.dumps(sys.argv[1:]) + "\n")
    if sys.argv[2:] == ["--help"]:
        print(HELP.get(sys.argv[1], ""))
        sys.exit(0)
    sys.exit(VERBS[sys.argv[1]](sys.argv[2:]))
