"""Which Matrix thread belongs to which run, persisted across restarts.

A run's first attention event opens a thread; every later message about that
run is a threaded reply, and a human's reply carries the thread relation back
(spec D5). That relation is the whole addressing scheme — nobody pastes a run
id into a room — so the map from thread root to run has to outlive the process.
A bridge that forgot it would open a second thread for a run that already has
one, and would ignore replies in the first.

The file is ``~/.lmer/platform/matrix/threads.json``, written through
:func:`lmer_platform.store.write_json`: same atomic rename, same owner-only
mode, same schema stamp as every other snapshot the platform keeps. Reusing it
rather than writing a second atomic-write helper is the point — the *third*
implementation of "write a temp file and rename it" is where the subtle one
lives.

A run is keyed by ``(host, project, slug)``, which is the identity
``/api/state`` gives it and the one ``work`` uses. The mapping is stored as a
flat ``{event_id: "host/project/slug"}`` so the file reads as what it is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Optional

from lmer_platform.store import ensure_state_dir, read_json, write_json

#: The filename inside :attr:`matrix_bridge.config.MatrixConfig.state_dir`.
THREADS_FILENAME = "threads.json"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunKey:
    """A run's identity, as ``/api/state`` reports it."""

    host: str
    project: str
    slug: str

    def __str__(self) -> str:
        return f"{self.host}/{self.project}/{self.slug}"

    @classmethod
    def parse(cls, text: str) -> "RunKey":
        """``host/project/slug``, where the project itself contains slashes.

        Split from both ends rather than by count: a GitLab project is a path
        (``group/project``, ``group/team/project``) and only the first and
        last segments are fixed.
        """
        parts = text.split("/")
        if len(parts) < 3:
            raise ValueError(
                f"{text!r} is not a run key (host/project/slug, project may "
                f"contain slashes)"
            )
        return cls(parts[0], "/".join(parts[1:-1]), parts[-1])

    @classmethod
    def from_row(cls, row: Mapping) -> "RunKey":
        """The key of one ``/api/state`` run row."""
        return cls(row["host"], row["project"], row["slug"])


class ThreadMap:
    """Thread root ↔ run, loaded once and written on every change.

    Written eagerly rather than at shutdown: the fact worth keeping is "this run
    already has a thread", and the moment it becomes true is the moment a
    crash would otherwise lose it — leaving the next start to announce a run
    the room is already showing.
    """

    def __init__(self, path: Path, mapping: Optional[Mapping[str, str]] = None):
        self.path = path
        self._by_root: dict[str, RunKey] = {}
        self._by_run: dict[str, str] = {}
        for root, key in (mapping or {}).items():
            try:
                run = RunKey.parse(key)
            except ValueError:
                # The load contract is "a corrupt file costs a duplicate thread,
                # never a bridge that will not start" (!243 review): the type
                # filter in `load` checked that the value is a string, and a
                # string that is not a run key raised here anyway.
                logger.warning(
                    "matrix_thread_entry_unusable root=%s value=%r", root, key,
                )
                continue
            self._remember(root, run)

    @classmethod
    def load(cls, path: Path) -> "ThreadMap":
        """Read the file, or start empty when there is none.

        A corrupt file is not fatal: :func:`~lmer_platform.store.read_json` has
        already moved it aside, and an empty map costs one duplicate thread per
        waiting run — where refusing to start costs every notification until
        someone notices. The loss is logged by the store.
        """
        try:
            data = read_json(path)
        except Exception:
            data = None
        threads = (data or {}).get("threads") or {}
        return cls(path, {
            root: key for root, key in threads.items()
            if isinstance(root, str) and isinstance(key, str)
        })

    def bind(self, root_event_id: str, run: RunKey) -> None:
        """Record that *root_event_id* is *run*'s thread, and persist it.

        Re-binding a run to a new root is allowed and replaces the old entry:
        a room the operator cleared, or a thread that was redacted, should not
        leave the bridge unable to ever announce that run again.
        """
        self._remember(root_event_id, run)
        self.save()

    def forget(self, run: RunKey) -> None:
        """Drop *run*'s thread, and persist it. Silent when there is none."""
        root = self._by_run.pop(str(run), None)
        if root is not None:
            self._by_root.pop(root, None)
            self.save()

    def run_for(self, root_event_id: str) -> Optional[RunKey]:
        """The run whose thread this is, or ``None`` — never a guess."""
        return self._by_root.get(root_event_id)

    def root_for(self, run: RunKey) -> Optional[str]:
        """The thread this run already has, or ``None``."""
        return self._by_run.get(str(run))

    def save(self) -> None:
        ensure_state_dir(self.path.parent)
        write_json(self.path, {
            "threads": {root: str(key) for root, key in self._by_root.items()},
        })

    def _remember(self, root_event_id: str, run: RunKey) -> None:
        previous_root = self._by_run.get(str(run))
        if previous_root is not None and previous_root != root_event_id:
            self._by_root.pop(previous_root, None)
        self._by_root[root_event_id] = run
        self._by_run[str(run)] = root_event_id

    def __len__(self) -> int:
        return len(self._by_root)

    def __iter__(self) -> Iterator[tuple]:
        return iter(tuple((root, key) for root, key in self._by_root.items()))
