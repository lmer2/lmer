"""The daemon's side of a session's thread requests (issue #414).

One :class:`lmer_cli.thread_access.ThreadServicer` per live session, reconciled
the way :class:`CommsHub` reconciles its subscribers. What the daemon adds over
the bare component is what only it knows: the run's fleet-view title, the
session's upload store, and — since issue #429 — the connections it keeps for
that session, which is what lets a run open and connect its own threads.
``docs/COMMS.md`` §12 has the rest, including why this is a thread of the daemon
rather than a process per session as a connector is.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from lmer_cli.thread_access import ThreadServicer, UploadStore

from . import references, registry, runs, session_io, session_threads, uploads

logger = logging.getLogger(__name__)

__all__ = ["ThreadServices", "register_services", "get_services"]

#: The reconcile cadence the rest of the daemon uses.
RECONCILE_SECONDS = 5.0


class ThreadServices:
    """The set of running servicers, kept equal to the live sessions."""

    def __init__(self, *, environ: Optional[dict] = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)
        self._servicers: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._reconcile_forever, name="lmer-thread-services", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            servicers = list(self._servicers.values())
            self._servicers.clear()
        for servicer in servicers:
            servicer.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _reconcile_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception as exc:  # one bad tick must not end the service
                logger.warning("platform_thread_services_reconcile_failed error=%s", exc)
            self._stop.wait(RECONCILE_SECONDS)

    # -- the set ---------------------------------------------------------------

    def reconcile(self) -> None:
        """A servicer for every live session that has a control plane, and none
        for a session that has gone. Also the tick that tells a run about a
        thread it has been pointed at."""
        live = set()
        for entry in registry.list_sessions(live_only=True):
            session_id = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(session_id, str) or not session_id:
                continue
            live.add(session_id)
            self.ensure(session_id)
        with self._lock:
            gone = [key for key in self._servicers if key not in live]
        for session_id in gone:
            self.drop(session_id)
        self.announce(live)

    # -- telling a run what it has been pointed at (issue #414) ----------------

    def announce(self, sessions) -> int:
        """Tell each live run about the reference threads it has not heard of.

        Here rather than in the route that writes one, because a reference can be
        attached before the session exists. Told once in two ways that have to
        agree: marked only after the bus takes the record, and the record carries
        a ``ref`` the bus dedupes on.
        """
        stored = references.load_all()
        if not stored:
            return 0
        told = 0
        for session_id in sorted(sessions):
            identity = self._run_of(session_id)
            if identity is None or not stored.get(runs.run_key(*identity)):
                continue
            for reference in references.pending(*identity):
                if not self._tell(session_id, reference):
                    break
                references.mark_announced(*identity, reference.id)
                told += 1
        return told

    def _run_of(self, session_id: str):
        try:
            return runs.run_for_session(session_id)
        except Exception:
            return None

    def _tell(self, session_id: str, reference) -> bool:
        from .comms import ensure_hub

        try:
            ensure_hub().post(
                session_id,
                kind="message",
                text=references.announcement(reference),
                channel="platform",
                author=reference.added_by or "operator",
                # The bus dedupes on (channel, ref), so a record this daemon
                # thinks it has not sent is still posted only once.
                ref=f"reference:{reference.id}",
            )
            return True
        except Exception as exc:
            logger.debug(
                "platform_reference_not_announced session=%s reference=%s error=%s",
                session_id, reference.id, exc,
            )
            return False

    def ensure(self, session_id: str) -> None:
        with self._lock:
            servicer = self._servicers.get(session_id)
            if servicer is not None and servicer.is_alive():
                return
            if self._stop.is_set():
                return
            # Before the endpoint call below, because the re-check under the
            # lock at the end asks "did somebody else start one" and a dead entry
            # left here answers yes forever.
            self._servicers.pop(session_id, None)
        try:
            endpoint = session_io.control_endpoint(session_id)
        except session_io.SessionIOError:
            # No control plane, or a session the registry no longer has: there is
            # nothing to poll, and the next tick asks again.
            return
        servicer = ThreadServicer(
            supervisor_url=f"http://{endpoint.host}:{endpoint.port}",
            token=endpoint.token,
            label=_run_label_of(session_id),
            run_name=_run_name_of(session_id),
            env=self._environ,
            store=self._store(session_id),
            # A run asking about its *own* threads: the daemon keeps them, so
            # only its servicers carry this (issue #429).
            connections=session_threads.SessionThreads(
                session_id, env=self._environ,
            ),
            name=f"lmer-thread-servicer-{session_id}",
            # The session is already up; the connector's cold-image budget would
            # hold a thread for a supervisor that is simply gone.
            connect_budget=RECONCILE_SECONDS,
        )
        with self._lock:
            if session_id in self._servicers or self._stop.is_set():
                return
            self._servicers[session_id] = servicer
        servicer.start()

    def drop(self, session_id: str) -> None:
        with self._lock:
            servicer = self._servicers.pop(session_id, None)
        if servicer is not None:
            servicer.stop_event.set()

    def sessions(self) -> list:
        with self._lock:
            return sorted(self._servicers)

    # -- what the daemon knows and the component does not ----------------------

    def _store(self, session_id: str) -> Optional[UploadStore]:
        try:
            host_dir = uploads.mounted_store(session_id, registry.read_session(session_id))
        except Exception:
            host_dir = None
        if host_dir is None:
            return None
        return UploadStore.of(str(host_dir), uploads.CONTAINER_UPLOADS_DIR)


def _run_name_of(session_id: str):
    """The run's own name for the machine-readable stamp (#427): its title, else
    its slug, else nothing.

    Not ``_run_label_of``, which falls back to ``"lmer session <id>"`` — that is
    the line humans read in the thread, and `Poster.author` composes the "lmer"
    word itself, so handing it that label spelled it twice (review of !300).
    """
    def name() -> str:
        from . import meta, runs

        try:
            identity = runs.run_for_session(session_id)
            if identity is None:
                return ""
            return meta.read(*identity).title or identity[2] or ""
        except Exception:
            return ""

    return name


def _run_label_of(session_id: str):
    """How a thread is told which run posted into it.

    A callable, because a run is frequently named *after* it starts. The label
    itself is :func:`lmer_platform.session_threads.run_label`, which an opened
    thread's root message uses for the same audience.
    """
    return lambda: session_threads.run_label(session_id)


_services: Optional[ThreadServices] = None


def register_services(services: Optional[ThreadServices]) -> None:
    global _services
    _services = services


def get_services() -> Optional[ThreadServices]:
    return _services
