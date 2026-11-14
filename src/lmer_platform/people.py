"""Who a bus record speaks for: the platform's person registry (issue #604).

``config.json`` lists the people whose time this platform tracks (spec D2):

    "people": [{"key": "alice", "name": "Alice", "slack": ["U0123"],
                "matrix": ["@alice:example.org"]}],
    "default_person": "alice"

Only a listed identity maps to a person, so another human in a shared thread
never drives someone else's timer. The web chat and the web UI act for
``default_person`` (D9): a ``platform`` record the daemon stamped as the
operator credential's (:data:`META_CREDENTIAL`) that names no author.
``docs/TIME-TRACKING.md`` has the rest.

:func:`parse_registry` is the one rule set: :func:`lmer_platform.config.load`
runs it to warn and disable tracking, and :meth:`Registry.from_config`
runs it to build the lookup, so the two can never disagree about what is valid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from lmer_cli.comms import (
    CHANNEL_MATRIX, CHANNEL_PLATFORM, CHANNEL_SLACK, MAX_AUTHOR_CHARS, Message,
)
from lmer_timesheet import RESERVED_PERSON_KEYS, is_person_key, is_slug

__all__ = [
    "RESERVED_PERSON_KEYS", "OPERATOR_AUTHOR", "META_CREDENTIAL", "CREDENTIAL_OPERATOR",
    "CREDENTIAL_CONNECTOR", "Person", "Registry", "parse_registry", "identity_of",
]

MAX_PERSON_KEY_CHARS = 64
#: The author ``lmer_platform.api._author_of`` stamps on an operator-credential
#: post that names nobody: the web chat and the web UI.
OPERATOR_AUTHOR = "operator"
#: The ``meta`` key the daemon stamps on a record posted through its API: the
#: kind of credential behind the post, never the caller's own copy.
META_CREDENTIAL = "credential"
CREDENTIAL_OPERATOR = "operator"
CREDENTIAL_CONNECTOR = "connector"
_MXID_RE = re.compile(r"^@[^\s:]+:\S+$")
_TRANSPORTS = (CHANNEL_SLACK, CHANNEL_MATRIX)


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    slack: tuple = ()
    matrix: tuple = ()


def parse_registry(people: object, default_person: object) -> tuple:
    """``(persons, default_person, problems)`` for a ``config.json`` registry.

    *problems* is a list of sentences; when it is not empty the registry is not
    usable and *persons* is ``()`` and *default_person* ``None``. Unknown keys
    in a person entry are ignored, as unknown top-level keys are. With one
    person listed and no *default_person* given, that person is the default.
    """
    problems: list = []
    if people is None:
        people = []
    if not isinstance(people, (list, tuple)):
        problems.append(f"people must be a list of person entries, got {type(people).__name__}")
        people = []
    persons: list = []
    keys: set = set()
    owners: dict = {}
    for index, entry in enumerate(people):
        where = f"people[{index}]"
        if not isinstance(entry, Mapping):
            problems.append(f"{where} must be an object with a key, got {type(entry).__name__}")
            continue
        key = entry.get("key")
        reason = _key_reason(key)
        if reason:
            problems.append(f"{where}.key {key!r} {reason}")
            continue
        where = f"person {key!r}"
        if key in keys:
            problems.append(f"{where} is listed more than once")
            continue
        keys.add(key)
        name = entry.get("name")
        if name is None:
            name = key
        elif not isinstance(name, str) or not name.strip():
            problems.append(f"{where}: name must be non-empty text")
            name = key
        identities = {}
        for transport in _TRANSPORTS:
            ids, bad = _identities(entry.get(transport), transport)
            problems.extend(f"{where}: {problem}" for problem in bad)
            for raw in ids:
                owner = owners.setdefault((transport, raw), key)
                if owner != key:
                    problems.append(
                        f"{transport} identity {raw!r} is listed under both "
                        f"{owner!r} and {key!r}; an identity belongs to one person"
                    )
            identities[transport] = ids
        persons.append(Person(
            key=key, name=name.strip(),
            slack=identities[CHANNEL_SLACK], matrix=identities[CHANNEL_MATRIX],
        ))
    default = None
    if default_person is not None:
        if not isinstance(default_person, str):
            problems.append(f"default_person must be a person key, got {type(default_person).__name__}")
        elif default_person.strip():
            default = default_person.strip()
            if default not in keys:
                problems.append(f"default_person {default!r} is not a listed person")
    if default is None and len(persons) == 1:
        default = persons[0].key
    if problems:
        return (), None, problems
    return tuple(persons), default, []


def _key_reason(key: object) -> Optional[str]:
    if not is_slug(key):
        return "is not a slug (lowercase letters, digits, '.', '_' or '-', starting with a letter or digit)"
    if len(key) > MAX_PERSON_KEY_CHARS:
        return f"is longer than {MAX_PERSON_KEY_CHARS} characters"
    if not is_person_key(key):
        return f"is reserved ({', '.join(sorted(RESERVED_PERSON_KEYS))} name work-repo directories)"
    return None


def _identities(value: object, transport: str) -> tuple:
    """``(ids, problems)`` for one person's list of *transport* identities."""
    if value is None:
        return (), []
    if not isinstance(value, (list, tuple)):
        return (), [f"{transport} must be a list of ids, got {type(value).__name__}"]
    ids: list = []
    problems: list = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip() or any(c.isspace() for c in raw.strip()):
            problems.append(f"{transport} id {raw!r} must be text without spaces")
            continue
        raw = raw.strip()
        if len(raw) > MAX_AUTHOR_CHARS:
            problems.append(f"{transport} id {raw[:40]!r}… is longer than {MAX_AUTHOR_CHARS} characters")
            continue
        if transport == CHANNEL_MATRIX and not _MXID_RE.match(raw):
            problems.append(f"matrix id {raw!r} is not an MXID (@user:server)")
            continue
        if raw not in ids:
            ids.append(raw)
    return tuple(ids), problems


class Registry:
    """The lookup from a channel identity to a person key."""

    def __init__(self, persons: tuple = (), default_person: Optional[str] = None,
                 problems: tuple = ()):
        self.persons = tuple(persons)
        self.default_person = default_person
        self.problems = tuple(problems)
        self._by_identity: dict = {}
        for person in self.persons:
            for raw in person.slack:
                self._by_identity[(CHANNEL_SLACK, raw)] = person.key
            for raw in person.matrix:
                self._by_identity[(CHANNEL_MATRIX, raw)] = person.key
        if default_person:
            self._by_identity[(CHANNEL_PLATFORM, OPERATOR_AUTHOR)] = default_person

    @classmethod
    def from_config(cls, config) -> "Registry":
        """The registry *config* declares; maps nobody while tracking is off.

        Off is :attr:`lmer_platform.config.PlatformConfig.time_problems`, which
        also counts unusable time settings; the registry is re-checked here too
        for a config that did not come through ``load()``.
        """
        persons, default, problems = parse_registry(
            getattr(config, "people", None), getattr(config, "default_person", None),
        )
        known = tuple(getattr(config, "time_problems", ()) or ())
        problems = known + tuple(problem for problem in problems if problem not in known)
        if problems:
            return cls((), None, tuple(problems))
        return cls(persons, default)

    @property
    def enabled(self) -> bool:
        return not self.problems

    def person(self, key: str) -> Optional[Person]:
        return next((person for person in self.persons if person.key == key), None)

    def person_for(self, message: object) -> Optional[str]:
        """The person key *message* speaks for, or ``None``."""
        identity = _identity(message)
        return self._by_identity.get(identity) if identity else None

    def person_for_sender(self, transport: object, raw_id: object) -> Optional[str]:
        """The person behind a conversation request's ``(transport, sender)``."""
        if transport not in _TRANSPORTS or not isinstance(raw_id, str):
            return None
        return self._by_identity.get((transport, raw_id.strip()))

    def lists(self, identity: str) -> bool:
        """Whether a listed person owns *identity*, written ``channel:id``."""
        channel, _, raw = identity.partition(":")
        return (channel, raw) in self._by_identity

    def unmapped(self, message: object) -> Optional[str]:
        """``channel:id`` for a human record no listed person owns, else ``None``.

        ``None`` too for a record that is not a human's (an assistant, a
        supervisor, a daemon announcement) or that carries no stable id.
        """
        identity = _identity(message)
        if identity is None or identity in self._by_identity:
            return None
        return f"{identity[0]}:{identity[1]}"


def identity_of(message: object) -> Optional[tuple]:
    """``(channel, id)`` naming the human behind *message*, listed or not (issue #668)."""
    return _identity(message)


def _identity(message: object) -> Optional[tuple]:
    """``(channel, id)`` naming the human behind *message*, or ``None``.

    Takes a :class:`lmer_cli.comms.Message` or the record's dict form, which is
    what the hub's events carry.
    """
    if isinstance(message, Message):
        source = message.source
        channel, author, ref = source.channel, source.author, source.ref
        author_id = getattr(source, "author_id", None)
        meta = message.meta
    elif isinstance(message, Mapping):
        source = message.get("source")
        if not isinstance(source, Mapping):
            return None
        channel, author, ref = source.get("channel"), source.get("author"), source.get("ref")
        author_id = source.get("author_id")
        meta = message.get("meta")
    else:
        return None
    author_id = author_id.strip() if isinstance(author_id, str) and author_id.strip() else None
    meta = meta if isinstance(meta, Mapping) else {}
    credential = meta.get(META_CREDENTIAL)
    mxid = author.strip() if isinstance(author, str) and _MXID_RE.match(author.strip()) else None
    if channel == CHANNEL_SLACK:
        return (CHANNEL_SLACK, author_id) if author_id else None
    if channel == CHANNEL_MATRIX:
        if author_id:
            return (CHANNEL_MATRIX, author_id)
        # A Matrix connector process started before it kept ``author_id``
        # puts the MXID in the author. Through the daemon's API
        # the author is whatever the caller typed.
        return (CHANNEL_MATRIX, mxid) if mxid and credential is None else None
    if channel == CHANNEL_PLATFORM:
        # The daemon announces on this channel too (a thread reference, a
        # connector's health); those carry a ref or a notification class and
        # are nobody's activity.
        if credential != CREDENTIAL_OPERATOR or ref or meta.get("notification_class"):
            return None
        if author == OPERATOR_AUTHOR:
            return (CHANNEL_PLATFORM, OPERATOR_AUTHOR)
        # The Matrix bridge answers with the operator credential and names
        # the sender it authenticated.
        if mxid:
            return (CHANNEL_MATRIX, mxid)
    return None
