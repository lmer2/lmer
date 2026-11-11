"""Human time tracking's running parts, and applying a new configuration to them
without a daemon restart (issues #604, #668; docs/TIME-TRACKING.md).

The daemon starts one :class:`TimeTracking` and puts it on the comms hub for its
whole life. It forwards each record to the activity consumer while there is
one, so tracking can start and stop under a running hub. :meth:`TimeTracking.apply`
is what a settings write calls. It swaps the registry, ``default_person``, the
switch and the writer identity into the running parts, and starts or stops the
parts when tracking turns on or off. Tracking that ends for someone (switched
off, or the person removed) ends their running timer first.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import Optional

from .config import TIME_INT_SETTINGS, TIME_STR_SETTINGS, PlatformConfig
from .people import Registry
from .timesheet_writer import TimesheetWriter, register_writer
from .timetrack import Tracker, register_tracker
from .timetrack_activity import ActivityConsumer, register_activity

logger = logging.getLogger(__name__)

__all__ = ["TimeTracking", "STARTED", "STOPPED", "UPDATED", "OFF", "register_tracking",
           "get_tracking", "current_config"]

#: The :class:`PlatformConfig` fields a settings write can change. Only these
#: are taken from the freshly loaded config: the rest of the daemon's config
#: carries its command-line overrides, which a bare ``load()`` does not see.
TIME_FIELDS = ("people", "default_person", "time_tracking", "time_problems",
               *TIME_INT_SETTINGS, *TIME_STR_SETTINGS)

#: What :meth:`TimeTracking.apply` did.
STARTED = "started"
STOPPED = "stopped"
UPDATED = "updated"
OFF = "off"


class TimeTracking:
    """The tracker, the timesheet writer and the activity consumer, as one unit."""

    def __init__(self) -> None:
        self.tracker: Optional[Tracker] = None
        self.writer: Optional[TimesheetWriter] = None
        self.activity: Optional[ActivityConsumer] = None
        self.config: Optional[PlatformConfig] = None
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self.tracker is not None

    def __call__(self, event) -> None:
        """The hub's consumer: the activity consumer's, while there is one."""
        activity = self.activity
        if activity is not None:
            activity(event)

    def start(self, config: PlatformConfig) -> "TimeTracking":
        """Start the parts *config* asks for. Never fatal: a part that fails to
        start is logged, everything started so far is stopped again, and the
        daemon serves without tracking."""
        with self._lock:
            self.config = config
            if config.time_problems:
                logger.warning("platform_time_tracking_disabled problems=%s",
                               "; ".join(config.time_problems))
                return self
            if not config.time_tracking_runs:
                reason = "time_tracking is off" if not config.time_tracking else "no people listed"
                logger.info("platform_time_tracking_off reason=%s in config.json", reason)
                return self
            try:
                self.tracker = Tracker(config)
                register_tracker(self.tracker)
                writer = TimesheetWriter(config)
                writer.start()
                self.writer = writer
                register_writer(writer)
                activity = ActivityConsumer(config, self.tracker, Registry.from_config(config))
                activity.start()
                self.activity = activity
                register_activity(activity)
            except Exception as exc:
                logger.warning("platform_time_tracking_start_failed error=%s", exc)
                self._stop_parts()
            return self

    def stop(self) -> None:
        with self._lock:
            self._stop_parts()

    def _stop_parts(self) -> None:
        # The activity first: its last decisions may queue entries for the writer.
        activity, self.activity = self.activity, None
        writer, self.writer = self.writer, None
        for part, unregister in ((activity, register_activity), (writer, register_writer)):
            if part is None:
                continue
            try:
                part.stop()
            except Exception as exc:
                logger.warning("platform_time_tracking_stop_failed part=%s error=%s",
                               type(part).__name__, exc)
            unregister(None)
        if self.tracker is not None:
            register_tracker(None)
        self.tracker = None

    def _close_timers(self, people) -> None:
        try:
            self.tracker.stop_running(people)
        except Exception as exc:
            logger.warning("platform_time_tracking_close_timers_failed error=%s", exc)

    def reload(self, load) -> str:
        """:meth:`apply` to ``load()``, read under the lock so writes apply in order."""
        with self._lock:
            return self.apply(load())

    def apply(self, config: PlatformConfig) -> str:
        """Bring the running parts in line with *config* and say what that took:
        :data:`STARTED`, :data:`STOPPED`, :data:`UPDATED` or :data:`OFF`."""
        with self._lock:
            if self.config is not None:
                config = replace(self.config, **{name: getattr(config, name) for name in TIME_FIELDS})
            if not config.time_tracking_runs or config.time_problems:
                was = self.running
                if was:
                    # Before the parts stop, while there is a tracker: a timer left
                    # open would accrue the off period, and nobody could stop it.
                    self._close_timers(None)
                self._stop_parts()
                self.start(config)
                return STOPPED if was else OFF
            if not self.running:
                self.start(config)
                return STARTED if self.running else OFF
            listed = {person.key for person in Registry.from_config(config).persons}
            removed = {person.key for person in self.activity.registry.persons} - listed
            if removed:
                self._close_timers(removed)
            self.tracker.resume(listed)
            self.config = config
            self.tracker.config = config
            self.writer.config = config
            self.activity.config = config
            self.activity.registry = Registry.from_config(config)
            return UPDATED


_TRACKING: Optional[TimeTracking] = None
_TRACKING_LOCK = threading.Lock()


def register_tracking(tracking: Optional[TimeTracking]) -> None:
    global _TRACKING
    with _TRACKING_LOCK:
        _TRACKING = tracking


def get_tracking() -> Optional[TimeTracking]:
    with _TRACKING_LOCK:
        return _TRACKING


def current_config(fallback: PlatformConfig) -> PlatformConfig:
    """The configuration tracking last applied, else *fallback* (the app's own)."""
    tracking = get_tracking()
    if tracking is not None and tracking.config is not None:
        return tracking.config
    return fallback
