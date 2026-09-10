"""Launch and supervise connector children without restarting their session harness."""
from __future__ import annotations

import contextlib
import errno
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ask_channel import bus

from .connector_delivery import RecoveryRequired, marker_path
from .connector_lifecycle import DRAIN_SECONDS
from .connector_ownership import (
    LEASE_DIRECTORY_ENV, LEASE_FD_ENV, LEASE_TOKEN_ENV, READY_FD_ENV,
    Lease, OwnershipBusy, OwnershipError, ThreadKey, read_json, same_process, write_json,
)

logger = logging.getLogger(__name__)


def child_environment(base=None):
    from .cli import _updater_child_env

    trusted = _updater_child_env()
    env = {**trusted, **(base or {})}
    env["PYTHONPATH"] = trusted["PYTHONPATH"]
    env["PYTHONSAFEPATH"] = "1"
    for name in ("PYTHONHOME", "PYTHONSTARTUP", LEASE_FD_ENV, LEASE_TOKEN_ENV,
                 READY_FD_ENV, LEASE_DIRECTORY_ENV):
        env.pop(name, None)
    return env


def spawn_child(argv, env, log_path=None, *, pass_fds=()):
    stream = subprocess.DEVNULL
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "ab")
    try:
        return subprocess.Popen(
            argv, env=env, pass_fds=pass_fds, cwd=os.path.dirname(sys.executable),
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        if stream is not subprocess.DEVNULL:
            stream.close()


def terminate_wait(process, timeout=DRAIN_SECONDS + 2):
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    return process.wait(timeout=5)


@dataclass
class OwnedProcess:
    popen: object
    lease: Lease

    def stop(self, timeout=DRAIN_SECONDS + 2):
        code = terminate_wait(self.popen, timeout)
        self.lease.close()
        return code

    def collect(self):
        code = self.popen.wait(timeout=0)
        self.lease.close()
        return code


def spawn_owned(key, argv, env, *, owner, session=None, permalink=None,
                runner=spawn_child, log_path=None):
    lease = Lease.acquire(key, owner=owner, session=session, permalink=permalink)
    ready_read = ready_write = None
    process = None
    try:
        if marker_path(key).exists():
            raise RecoveryRequired("connector requires explicit recovery of its pending send")
        ready_read, ready_write = os.pipe()
        child_env = child_environment(env)
        child_env.update(lease.environment(ready_read))
        process = runner(argv, child_env, log_path, pass_fds=(lease.fd, ready_read))
        lease.attach_child(process.pid)
        os.write(ready_write, b"1")
        return OwnedProcess(process, lease)
    except BaseException:
        try:
            if process is not None:
                terminate_wait(process)
        finally:
            lease.close(release=process is None or process.poll() is not None)
        raise
    finally:
        for fd in (ready_read, ready_write):
            if fd is not None:
                os.close(fd)


def reclaim_orphan(key: ThreadKey, *, session: str | None, canonical: str) -> bool:
    """Signal only the exact, validated child of a dead owner. Never acquire over it."""
    with key.metadata_lock():
        if not key.busy():
            return False
        entry = read_json(key.owner_path) or {}
        if entry.get("thread") != key.parts or not session or entry.get("session") != session:
            raise OwnershipBusy("thread belongs to another or unidentified supervisor session")
        if same_process(entry.get("parent_pid", 0), entry.get("parent_start")):
            raise OwnershipBusy("the connector's parent owner is still alive")
        pid = entry.get("child_pid")
        if not isinstance(pid, int) or not same_process(pid, entry.get("child_start")):
            raise OwnershipBusy("live lease has no verifiable connector child identity")
        try:
            descriptor = os.pidfd_open(pid)
        except OSError as exc:
            if exc.errno in (errno.ENOSYS, errno.EPERM, errno.EACCES):
                raise OwnershipBusy("pidfd signaling unavailable; waiting for the orphan to drain itself") from exc
            raise
        try:
            if not same_process(pid, entry.get("child_start")):
                raise OwnershipBusy("connector process identity changed")
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            module = f"{key.transport}_chat.connector".encode()
            expected = canonical.encode()
            if not any(argv[i:i + 2] == [b"-m", module] for i in range(len(argv) - 1)) or not any(
                argv[i:i + 2] == [b"--permalink", expected] for i in range(len(argv) - 1)
            ):
                raise OwnershipBusy("live process is not the recorded connector for this thread")
            stopped = entry.get("stop_requested_at")
            if stopped is None:
                entry["stop_requested_at"] = time.time()
                write_json(key.owner_path, entry)
            sig = signal.SIGKILL if stopped is not None and time.time() - stopped >= DRAIN_SECONDS + 2 else signal.SIGTERM
            signal.pidfd_send_signal(descriptor, sig)
            return True
        finally:
            os.close(descriptor)


class ConnectorProcess:
    def __init__(self, channel, conversation, root, permalink, endpoint, uploads=None, *, runner=spawn_child):
        self.key = ThreadKey(channel, conversation, root)
        self.permalink, self.endpoint, self.uploads = permalink, endpoint, uploads
        self.runner = runner
        self._stop = threading.Event()
        self._thread = None
        self._child = None
        self.status = "starting"
        self._resolved = False

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._supervise, name="lmer-connector-owner", daemon=True)
            self._thread.start()

    def _resolve(self):
        from .thread_access import resolve_thread

        ref, _, _ = resolve_thread(self.permalink)
        self.key = ThreadKey(ref.channel, *ref.registry_key())
        self.permalink = ref.canonical
        self._resolved = True

    def _session(self):
        try:
            identity = bus.BusClient(bus.Endpoint(*self.endpoint)).health().get("session")
            return identity if isinstance(identity, str) and identity else None
        except bus.BusError:
            return None

    def _spawn(self):
        if not self._resolved:
            self._resolve()
        env = child_environment()
        env["LMER_SUPERVISOR_URL"], env["LMER_FASTAPI_TOKEN"] = self.endpoint
        argv = [sys.executable, "-m", f"{self.key.transport}_chat.connector", "--permalink", self.permalink]
        if self.uploads:
            argv += ["--uploads-dir", self.uploads[0], "--container-uploads-dir", self.uploads[1]]
        session = self._session()
        try:
            return spawn_owned(self.key, argv, env, owner="direct", session=session,
                               permalink=self.permalink, runner=self.runner,
                               log_path=self.key.directory / "connector.log")
        except OwnershipBusy:
            reclaim_orphan(self.key, session=session, canonical=self.permalink)
            raise

    def _status(self, text):
        if text == self.status:
            return
        self.status = text
        logger.warning("direct_connector_status detail=%s", text)
        with contextlib.suppress(Exception):
            bus.BusClient(bus.Endpoint(*self.endpoint)).post(
                "note", text, source={"channel": self.key.transport},
            )

    def _supervise(self):
        failures = 0
        while not self._stop.is_set():
            try:
                self._child = self._spawn()
                self._status("connector running")
                while self._child.popen.poll() is None and not self._stop.wait(0.25):
                    pass
                if self._stop.is_set():
                    self._child.stop()
                    self._child = None
                    return
                code = self._child.collect()
                self._child = None
                if code == 0:
                    self._status("connector stopped")
                    return
                failures += 1
                self._status(f"connector exited ({code}); only its child will be restarted")
            except RecoveryRequired:
                self._status("connector recovery required; inspect the pending operation with lmer_cli.connector_recovery")
            except Exception as exc:
                logger.exception("direct_connector_failed")
                if self._child is not None:
                    self._child.stop()
                    self._child = None
                failures += 1
                self._status(f"connector retrying: {exc}")
            self._stop.wait(min(30, 2 ** min(failures, 5)))

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=DRAIN_SECONDS + 10)
            if self._thread.is_alive():
                raise RuntimeError("connector owner did not finish waiting for its child")
