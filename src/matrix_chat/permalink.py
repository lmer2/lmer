"""Matrix thread permalinks: a room (id or alias) and a thread-root event.

Two shapes are accepted, both as clients produce them:

- ``https://matrix.to/#/<room>/<event>[?via=…]`` — the matrix.to form, with
  ``<room>`` an id (``!abc:example.org``) or an alias (``#lmer:example.org``),
  each possibly percent-encoded (``%21abc%3Aexample.org``);
- ``https://<host>/#/room/<room>/<event>`` — the Element client form.

The event is the thread root: a link to a reply inside a thread names that
reply, and the connector resolves the root from it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlparse

__all__ = [
    "MatrixThread", "parse_matrix_permalink", "is_matrix_thread_url",
    "is_matrix_room", "matrix_to_link", "thread_tag", "thread_ref", "split_thread_ref",
]

_ROOM_RE = re.compile(r"^[!#][^/\s]+:[^/\s]+$")
_EVENT_RE = re.compile(r"^\$[^/\s?#]+$")
_TAGGED_REF_RE = re.compile(r"^([0-9a-f]{16}):(\$.+)$")


@dataclass(frozen=True)
class MatrixThread:
    room: str
    event_id: str

    @property
    def room_is_alias(self) -> bool:
        return self.room.startswith("#")


def _fragment_parts(url: str) -> Optional[list]:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("https", "http"):
        return None
    fragment = parsed.fragment or ""
    if not fragment:
        return None
    # `#/room/!id/$event` (Element) or `#/!id/$event` (matrix.to). The query
    # string (`?via=…`) rides on the fragment in matrix.to links.
    fragment = fragment.split("?", 1)[0]
    parts = [unquote(part) for part in fragment.strip("/").split("/") if part]
    if parts and parts[0] == "room":
        parts = parts[1:]
    return parts or None


def parse_matrix_permalink(url: str) -> Optional[MatrixThread]:
    parts = _fragment_parts(url)
    if not parts or len(parts) != 2:
        return None
    room, event = parts
    if not _ROOM_RE.match(room) or not _EVENT_RE.match(event):
        return None
    return MatrixThread(room=room, event_id=event)


def is_matrix_thread_url(value: object) -> bool:
    return isinstance(value, str) and parse_matrix_permalink(value) is not None


def is_matrix_room(value: object) -> bool:
    """Whether *value* is a room id or alias, by the reading a permalink uses.

    Which is the point of it being here: naming a room to open a thread in
    (issue #429) and naming one inside a link have to agree about what a room
    is, down to a server name carrying a port.
    """
    return isinstance(value, str) and bool(_ROOM_RE.match(value.strip()))


def matrix_to_link(room_id: str, event_id: str) -> str:
    """The canonical permalink for a thread, the shape the listener spawns with."""
    return f"https://matrix.to/#/{quote(room_id, safe='')}/{quote(event_id, safe='')}"


def thread_tag(room_id: str, thread_event: str) -> str:
    """A fixed-length name for one thread, which a ref can carry where the
    room id and root, colons and all, would not fit or split cleanly."""
    return hashlib.sha256(f"{room_id}\n{thread_event}".encode("utf-8")).hexdigest()[:16]


def thread_ref(room_id: str, thread_event: str, event_id: str) -> str:
    """A message's ``source.ref``: its thread's tag, then its event id."""
    return f"{thread_tag(room_id, thread_event)}:{event_id}"


def split_thread_ref(ref: str) -> tuple:
    """``(tag, event_id)`` of a :func:`thread_ref`; ``(None, ref)`` for the
    bare event id a connector from before the tag posts."""
    match = _TAGGED_REF_RE.match(ref)
    return (match.group(1), match.group(2)) if match else (None, ref)
