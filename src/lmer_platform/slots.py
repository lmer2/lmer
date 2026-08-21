"""Service slots: named bindings from a runner to a dev service on this host.

An operator declares slots in ``config.json``; each names a preset, and that
preset is what puts the session into service mode. Occupying a slot therefore
*is* running in service mode — neither can be had without the other.

Occupancy is derived, never stored. A slot reads occupied when a *live*
registry entry names it, and free the moment that entry stops being live. That
survives a daemon restart (liveness is a stateless PID probe) and cannot strand
a slot (a dead PID reads as free), where an ``occupied_by`` field would have
needed a reconciler for the states it could get into. It also keeps one
definition of liveness between here and the concurrency cap
(:func:`lmer_platform.spawn._live_worker_count`).

A slot's usability depends on a file it does not live in — the presets file —
so the two kinds of broken are handled differently. **Shape errors** (not a
mapping, no usable name, a duplicate) are the entry's own fault and cannot come
right on their own, so the entry is skipped with a warning. **Semantic errors**
leave the slot loaded and unusable *with a reason*, because most come right
when the presets file is fixed, and a slot that vanished would explain nothing
to the operator who typed its name.

The service probe is memoised for :data:`PROBE_TTL_SECONDS` because the fleet
payload is polled every ten seconds and a subprocess per slot per poll is a
cost a row does not need; the spawn path asks for a fresh answer instead, since
a poll can afford a stale one and an action taken on it cannot. The probe holds
no opinion about the container runtime — it passes one to
:func:`lmer_cli.service.resolve_container`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

# The real parser, so "do these args rebind the slot?" is answered by the thing
# that will actually decide it rather than by a second opinion about its grammar.
from lmer_cli.cli import ParseRefused, parse_args
from lmer_cli.presets import PRESETS_FILE_ENV, load_presets
from lmer_cli.service import (
    ServiceError,
    member_names,
    resolve_container,
    resolve_group,
)

from .config import PlatformConfig, _detected_runtime
from .registry import list_sessions

logger = logging.getLogger("lmer_platform.slots")

__all__ = [
    "SLOT_FREE", "SLOT_OCCUPIED", "SLOT_SERVICE_DOWN", "SLOT_MISCONFIGURED",
    "SLOT_STATES", "PROBE_TTL_SECONDS",
    "SlotDefinition", "SlotStatus",
    "parse_slots", "slot_definitions", "slot_status", "slot_rows",
    "probe_service", "probe_group", "group_members", "group_membership",
    "clear_probe_cache",
]

#: What a slot binds, and what a live session holds: ``("service", name)`` or
#: ``("group", compose project)``. A *pair* rather than a bare string because a
#: group session holds every member of its project (issue #312), so occupancy
#: became a set relation between two kinds of thing, and a namespaced key is
#: what keeps a compose project from colliding with a service of the same name.
_SERVICE = "service"
_GROUP = "group"


def _describe(binding: tuple) -> str:
    """A binding key in the words an operator uses for it."""
    kind, value = binding
    return (
        f"service group {value!r}" if kind == _GROUP else f"service {value!r}"
    )


#: Nothing holds the slot and its service is up — a spawn into it would run.
SLOT_FREE = "free"
#: A live session names this slot. The row carries which one.
SLOT_OCCUPIED = "occupied"
#: The definition is fine but the dev service it names is not running.
SLOT_SERVICE_DOWN = "service_down"
#: The definition names a preset this host cannot use. The row carries why.
SLOT_MISCONFIGURED = "misconfigured"

#: Every state a row may report, in the order a reader should think about them.
SLOT_STATES = (SLOT_FREE, SLOT_OCCUPIED, SLOT_SERVICE_DOWN, SLOT_MISCONFIGURED)

#: How long a service probe's answer is reused: longer than the fleet view's
#: ten-second poll, short enough that a service the operator just started shows
#: up while they are still looking at the screen.
PROBE_TTL_SECONDS = 30.0

#: Keys a slot entry may define. Anything else is logged rather than fatal, so a
#: key added by a newer build does not invalidate the slot on an older one.
_KNOWN_KEYS = frozenset({"name", "preset", "description"})


@dataclass(frozen=True)
class SlotDefinition:
    """One declared slot: a name, and the preset that gives it its service.

    ``preset`` is a name, not a resolved preset: what it means is the presets
    file's answer and can change without this definition changing, which is why
    an unknown preset makes the slot unusable rather than invalid.
    """

    name: str
    preset: str
    description: Optional[str] = None


@dataclass(frozen=True)
class SlotStatus:
    """A slot and everything known about it right now.

    Both the display state and the facts under it, because the readers want
    different things: the fleet view wants one word (:attr:`state`), while the
    spawn gate refuses in its own order — permanent before transient, each
    refusal naming its gate — and so reads the facts instead of
    reverse-engineering a precedence out of a single string.

    ``unusable_reason`` and ``service_down_reason`` are independent: a slot can
    be occupied *and* misconfigured when the definition changed under a running
    session.
    """

    definition: SlotDefinition
    #: The service the slot's preset targets, or ``None`` when it has none. For
    #: a group slot this is the member the session *starts* on, which may be
    #: ``None``: what the slot guards is :attr:`service_group`.
    service: Optional[str] = None
    #: The compose project the slot's preset attaches to, or ``None``. A group
    #: slot holds every running member of it (issue #312).
    service_group: Optional[str] = None
    #: For a group slot, the names its members can be addressed by, as the probe
    #: last read them. Empty for a single-service slot, and for a group whose
    #: project could not be read — where ``service_down_reason`` says why.
    members: tuple = ()
    #: Why this slot cannot be used at all, or ``None``. A configuration fault.
    unusable_reason: Optional[str] = None
    #: Every live session holding this slot, oldest first. More than one means
    #: the spawn race in :func:`lmer_platform.spawn._claim_slot` was lost, and
    #: the row has to say so rather than name one and look settled.
    occupants: tuple = ()
    #: Live sessions holding this slot's dev *service* under a **different**
    #: slot name, as ``(slot name, occupant)`` pairs. Measured from the running
    #: sessions rather than predicted from the presets file, because the file is
    #: hot and the case only arises when it has changed.
    service_occupants: tuple = ()
    #: Why the dev service is not usable, or ``None``.
    service_down_reason: Optional[str] = None
    #: The binding keys those occupants actually hold. A group slot can be
    #: blocked by its project or by any one member, and an operator reading
    #: "already in use" needs to know which container that is about.
    contested: tuple = ()

    @property
    def bindings(self) -> tuple:
        """What this slot takes when a session holds it.

        A group takes the project *and* every member: the project key is what a
        second group session collides on, and the member keys are what a
        single-service session already holding one of those containers collides
        on. Both directions, because the harm — two agents in one dev container
        — does not care which of them started first.
        """
        if self.service_group:
            return ((_GROUP, self.service_group),) + tuple(
                (_SERVICE, member) for member in self.members
            )
        if self.service:
            return ((_SERVICE, self.service),)
        return ()

    @property
    def occupant(self) -> Optional[dict]:
        """The holder a single-occupancy reader means, or ``None``.

        Readers that have to be truthful about a collision use
        :attr:`occupants` and :attr:`contended` instead.
        """
        return self.occupants[0] if self.occupants else None

    @property
    def contended(self) -> bool:
        """Whether more than one live session holds this slot."""
        return len(self.occupants) > 1

    @property
    def state(self) -> str:
        """The one word the row shows.

        Occupancy wins over a configuration fault: it is a fact about what is
        running rather than about what a file says. The fault is still reported
        in :attr:`reason`.
        """
        if self.occupant is not None:
            return SLOT_OCCUPIED
        # The resource a slot hands out is in use even though this slot's *name*
        # is free, so occupied is the truthful word.
        if self.service_occupants:
            return SLOT_OCCUPIED
        if self.unusable_reason is not None:
            return SLOT_MISCONFIGURED
        if self.service_down_reason is not None:
            return SLOT_SERVICE_DOWN
        return SLOT_FREE

    @property
    def service_busy_reason(self) -> Optional[str]:
        """Why this slot's service is unavailable to it, or ``None``."""
        if not self.service_occupants:
            return None
        who = ", ".join(
            f"{held.get('session_id')} (holding slot {slot!r})"
            for slot, held in self.service_occupants
        )
        return (
            f"{self._contested_description} is already in use by {who} — one "
            "service takes one slot"
        )

    @property
    def _contested_description(self) -> str:
        """What is taken, in the words that name the container in question."""
        binding = self.contested[0] if self.contested else (
            self.bindings[0] if self.bindings else None
        )
        if binding is None:
            return "service"
        if self.service_group and binding[0] == _SERVICE:
            # Blocked through one member rather than through the project: say
            # which, or the operator goes looking at the wrong container.
            return f"{_describe(binding)}, a member of service group {self.service_group!r},"
        return _describe(binding)

    @property
    def reason(self) -> Optional[str]:
        """Why this slot is not usable, whatever the state says."""
        return (
            self.unusable_reason
            or self.service_busy_reason
            or self.service_down_reason
        )

    @property
    def usable(self) -> bool:
        """Whether a spawn into this slot would get past the slot gates."""
        return self.state == SLOT_FREE

    def to_dict(self) -> dict:
        return {
            "name": self.definition.name,
            "preset": self.definition.preset,
            "description": self.definition.description,
            "service": self.service,
            "service_group": self.service_group,
            "members": list(self.members),
            "state": self.state,
            "reason": self.reason,
            "occupant": self.occupant,
            # Both, not just the first: "who has my dev service" answered with
            # one confident name would be wrong exactly where it matters.
            "occupants": list(self.occupants),
            # Non-empty says the service is taken though this slot's name is
            # free — the one case a name-keyed row would have called free.
            "service_occupants": [
                {"slot": slot, **held} for slot, held in self.service_occupants
            ],
        }


def parse_slots(raw: object) -> list[SlotDefinition]:
    """Turn the raw ``slots`` entries into definitions, skipping bad ones.

    Shape errors only. Nothing here consults the presets file: an entry naming
    a preset nobody has heard of is a well-formed definition of an unusable
    slot, and dropping it here is what would make it vanish.
    """
    if not isinstance(raw, (list, tuple)):
        if raw is not None:
            logger.warning(
                "slot_entries_invalid reason=not_a_list type=%s",
                type(raw).__name__,
            )
        return []

    definitions: list[SlotDefinition] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        definition = _build_slot(index, entry, seen)
        if definition is not None:
            seen.add(definition.name)
            definitions.append(definition)
    return definitions


def _build_slot(
    index: int, entry: object, seen: set[str]
) -> Optional[SlotDefinition]:
    """One entry as a definition, or ``None`` with a warning saying why."""
    if not isinstance(entry, dict):
        logger.warning(
            "slot_invalid index=%s reason=not_a_mapping type=%s",
            index, type(entry).__name__,
        )
        return None

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("slot_invalid index=%s reason=missing_name", index)
        return None
    name = name.strip()

    preset = entry.get("preset")
    if not isinstance(preset, str) or not preset.strip():
        logger.warning(
            "slot_invalid index=%s name=%s reason=missing_preset", index, name
        )
        return None
    preset = preset.strip()

    if name in seen:
        # First declaration wins, so the answer does not depend on how far down
        # the file a reader got. Loud, because the operator wrote two things and
        # only one is in effect.
        logger.warning("slot_invalid index=%s name=%s reason=duplicate", index, name)
        return None

    unknown = sorted(set(entry) - _KNOWN_KEYS)
    if unknown:
        logger.warning(
            "slot_unknown_keys name=%s keys=%s", name, ",".join(unknown)
        )

    description = entry.get("description")
    if description is not None and not isinstance(description, str):
        logger.warning(
            "slot_unknown_keys name=%s keys=description (not text, ignored)", name
        )
        description = None

    return SlotDefinition(name=name, preset=preset, description=description)


def slot_definitions(config: PlatformConfig) -> list[SlotDefinition]:
    """Every slot this host declares, in the order the file declares them."""
    return parse_slots(config.slots)


# --- the service probe ------------------------------------------------------

#: ``(runtime, kind, name) -> (deadline, reason or None, member names)``.
#: The members ride along because the group probe already resolved them and
#: occupancy needs them (issue #312). Guarded by
#: :data:`_PROBE_LOCK`: the daemon serves its routes from a threadpool, so two
#: polls can overlap.
_PROBE_CACHE: dict[tuple[str, str, str], tuple[float, Optional[str], tuple]] = {}
_PROBE_LOCK = threading.Lock()

#: Slot name → the session ids last warned about, so a collision warns on a
#: *transition* rather than on every ten-second poll. Guarded by
#: :data:`_PROBE_LOCK`, the module's one lock.
_WARNED_COLLISIONS: dict[str, frozenset] = {}


def clear_probe_cache() -> None:
    """Forget the module's memoised state: probe answers and warning dedup.

    Not called on a config reload — the cache is keyed on the service name, so
    a slot pointed at a different service simply misses.
    """
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()
        _WARNED_COLLISIONS.clear()
        _REBIND_CACHE.clear()


def probe_service(
    service: str,
    *,
    runtime: Optional[str] = None,
    cached: bool = True,
) -> Optional[str]:
    """Why *service* is not usable right now, or ``None`` when it is.

    With ``cached`` (the default) an answer younger than
    :data:`PROBE_TTL_SECONDS` is reused. The lock is released across the probe
    itself: holding it would serialise every other slot behind a slow query,
    and the worst a concurrent miss costs is the same query run twice.
    """
    return _probe((_SERVICE, service), runtime=runtime, cached=cached)


def probe_group(
    project: str,
    *,
    runtime: Optional[str] = None,
    cached: bool = True,
) -> Optional[str]:
    """Why compose project *project* is not usable right now, or ``None``.

    A group passes on **one** running member. Members of a live stack stop and
    start under it, and which of them are up is a question ``target-switch``
    answers at the moment of switching; the slot's question is only whether the
    stack is there at all.
    """
    return _probe((_GROUP, project), runtime=runtime, cached=cached)


def group_members(
    project: str, *, runtime: Optional[str] = None
) -> list[str]:
    """Every name a group session's containers can be addressed by, uncached.

    Both compose service names and container names (:func:`member_names`),
    because a single-service slot's preset may name either spelling and both
    reach a container this session can retarget to.

    Called once per group spawn to *record* what that session takes — never on
    the poll path, which is why it does not touch the probe cache.
    """
    if runtime is None:
        runtime = _detected_runtime()
    if runtime is None:
        raise ServiceError("no container runtime is available on this host")
    return member_names(resolve_group(runtime, project, announce=False))


def _probe(
    binding: tuple,
    *,
    runtime: Optional[str] = None,
    cached: bool = True,
) -> Optional[str]:
    """Why *binding* is not usable right now, or ``None``."""
    return _probe_full(binding, runtime=runtime, cached=cached)[0]


def group_membership(
    project: str,
    *,
    runtime: Optional[str] = None,
    cached: bool = True,
) -> tuple:
    """``(reason, member names)`` for a group, off the one probe.

    Occupancy needs the membership and the row needs the reason, and both come
    out of the same query — so they are one cached answer rather than two calls
    that could disagree about a stack that changed between them.
    """
    return _probe_full((_GROUP, project), runtime=runtime, cached=cached)


def _probe_full(
    binding: tuple,
    *,
    runtime: Optional[str] = None,
    cached: bool = True,
) -> tuple:
    """``(reason, members)``: the shared body of every probe above.

    ``members`` is empty for a service binding and for a group that did not
    resolve — a group nobody can read holds nothing, which is what leaves its
    row saying *why* rather than silently blocking every member on this host.
    """
    if runtime is None:
        runtime = _detected_runtime()
    if runtime is None:
        return "no container runtime is available on this host", ()

    kind, name = binding
    key = (runtime, kind, name)
    if cached:
        with _PROBE_LOCK:
            entry = _PROBE_CACHE.get(key)
        if entry is not None and entry[0] > time.monotonic():
            return entry[1], entry[2]

    members: tuple = ()
    try:
        if kind == _GROUP:
            members = tuple(
                member_names(resolve_group(runtime, name, announce=False))
            )
        else:
            resolve_container(runtime, name, announce=False)
        reason: Optional[str] = None
    except ServiceError as exc:
        # The resolver's own text, so the row can tell "nothing matched" from
        # "the runtime could not be queried". Flattened because the no-match
        # message lists the running containers and a row is one line.
        reason = " ".join(str(exc).split())

    # Deadline taken *after* the probe returns: the query can itself take the
    # runtime's ten-second timeout, and an answer dated from before it would
    # arrive having already spent a third of its life.
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = (time.monotonic() + PROBE_TTL_SECONDS, reason, members)
    return reason, members


# --- status -----------------------------------------------------------------


def _live_entries(sessions: Optional[list] = None) -> list:
    """The live registry entries occupancy is derived from.

    ``live_only`` is the whole occupancy rule: a dead session's entry survives
    on disk as evidence that it crashed, and counting one as an occupant is how
    a crash would strand a slot. A caller-supplied list is filtered here rather
    than trusted, because the fleet payload reads the registry with
    ``live_only=False`` to keep crashed runs visible.
    """
    if sessions is None:
        return list_sessions(live_only=True)
    return [
        entry for entry in sessions
        if isinstance(entry, dict) and entry.get("live")
    ]


def _occupant_of(entry: dict) -> dict:
    run = entry.get("run") if isinstance(entry.get("run"), dict) else {}
    return {
        "session_id": entry.get("id"),
        "host": run.get("host"),
        "project": run.get("project"),
        "slug": run.get("slug"),
    }


def _session_bindings(
    entry: dict, presets: dict, resolved: dict, name: str
) -> tuple:
    """Every binding a live session may hold, most exact first.

    What the gate recorded for this session is the whole answer wherever it
    exists: ``task.slot_service`` for a single service, and for a group
    (issue #312) ``task.slot_service_group`` plus ``task.slot_services`` — the
    members resolved at spawn. A group session yields **all** of them, because
    it can retarget to any of them without asking anyone.

    A *legacy* entry written before the gate recorded anything gets candidates
    instead — the current binding of the preset it names, and the slot's current
    resolution — rather than the first that matches: a legacy entry whose slot
    was repointed would otherwise have its block *moved* rather than widened,
    leaving the service it really holds reading free for the gate to grant.

    Not covered: if the *preset* was edited since the session launched, neither
    candidate is what that session holds and nothing on disk records it; and a
    container that joined the group after a group session started is switchable
    but unrecorded, so a slot on it reads free. Both are in
    ``docs/SERVICE-MODE.md`` under "Not in this slice".
    """
    task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
    recorded: list = []
    group = task.get("slot_service_group")
    if isinstance(group, str) and group:
        recorded.append((_GROUP, group))
        members = task.get("slot_services")
        if isinstance(members, list):
            recorded += [
                (_SERVICE, member) for member in members
                if isinstance(member, str) and member
            ]
    service = task.get("slot_service")
    if isinstance(service, str) and service:
        binding = (_SERVICE, service)
        if binding not in recorded:
            recorded.append(binding)
    if recorded:
        return tuple(recorded)

    candidates: list = []
    preset_name = task.get("preset")
    if isinstance(preset_name, str) and preset_name:
        preset = presets.get(preset_name)
        if preset is not None:
            for kind, attr in ((_GROUP, "service_group"), (_SERVICE, "service")):
                value = getattr(preset, attr, None)
                if value:
                    candidates.append((kind, value))
    for current in resolved.get(name) or ():
        if current not in candidates:
            candidates.append(current)
    return tuple(candidates)


def holders_of(pairs: list) -> list:
    """The occupants out of ``(slot name, occupant)`` pairs."""
    return [occupant for _, occupant in pairs]


def _held(
    sessions: Optional[list], presets: dict, resolved: dict
) -> tuple[dict, dict]:
    """``(by slot name, by dev service)`` for every live holder.

    Two views of one read: the name is what a session *claimed*, the service is
    what it actually took. They come apart exactly when the config changes under
    a running session, which the name-keyed view alone could not defend.

    **Every** holder is kept, not the first — more than one is precisely where
    reporting one would make the row that answers "who has my dev service"
    confidently wrong.
    """
    by_name: dict[str, list] = {}
    by_binding: dict[tuple, list] = {}
    for entry in _live_entries(sessions):
        name = entry.get("slot")
        if not isinstance(name, str) or not name:
            continue
        occupant = _occupant_of(entry)
        by_name.setdefault(name, []).append(occupant)
        # More than one for a group session (it holds every member) and for a
        # legacy entry (an inferred binding blocks every candidate, so the guess
        # widens the refusal instead of moving it).
        for binding in _session_bindings(entry, presets, resolved, name):
            holders = by_binding.setdefault(binding, [])
            if occupant not in holders_of(holders):
                holders.append((name, occupant))

    _warn_on_collisions(by_name)
    return (
        {name: tuple(held) for name, held in by_name.items()},
        {binding: tuple(held) for binding, held in by_binding.items()},
    )


def _warn_on_collisions(held: dict[str, list]) -> None:
    """Log each slot collision once per change in who is colliding."""
    current = {
        name: frozenset(
            h.get("session_id") for h in holders if h.get("session_id")
        )
        for name, holders in held.items()
        if len(holders) > 1
    }
    with _PROBE_LOCK:
        changed = {
            name: ids for name, ids in current.items()
            if _WARNED_COLLISIONS.get(name) != ids
        }
        _WARNED_COLLISIONS.clear()
        _WARNED_COLLISIONS.update(current)
    for name, ids in changed.items():
        logger.warning(
            "slot_double_occupancy name=%s sessions=%s — more than one live "
            "session holds this slot; they are running against one dev service",
            name, ",".join(sorted(ids)),
        )


#: What :func:`_rebinding_arg` says when a preset's ``args`` are ones ``lmer``
#: itself would refuse. Not "no rebinding": such a preset cannot produce a
#: working session either, so the slot is unusable for a stated reason.
ARGS_UNPARSEABLE = "unparseable"

#: ``(checkout, service, args) -> answer``. The full input, because the verdict
#: compares the parsed outcome against the preset's own fields — keying on the
#: args alone would serve one preset's answer to another whose fields differ.
#: Content-addressed, so it needs no TTL. Guarded by :data:`_PROBE_LOCK`.
_REBIND_CACHE: dict[tuple, Optional[str]] = {}


def _rebinding_arg(preset: object) -> Optional[str]:
    """Which of the slot's bindings the preset's ``args`` would re-decide.

    ``"--service"``, ``"--service-group"``, ``"--checkout"``,
    :data:`ARGS_UNPARSEABLE`, or ``None``.

    Decided by parsing and comparing **outcomes** rather than matching token
    spellings, because three cases defeat a spelling matcher:

    - ``lmer``'s parser leaves ``allow_abbrev`` on, so ``--che=/x`` rebinds
      exactly as ``--checkout`` does. Since ``--service-group`` joined the
      parser (issue #312) the ``--se``/``--serv`` family is ambiguous instead
      and argparse refuses it outright — :data:`ARGS_UNPARSEABLE`, a different
      reason for the same verdict, and one no spelling matcher would have got
      right either.
    - ``--service ""`` is a rebinding: ``lmer`` reads service mode off that
      value's truthiness, so the session would hold a service slot while running
      in ordinary mode — this module's opening invariant inverted.
    - ``--service web`` on a preset already bound to ``web`` is redundant config
      rather than a broken slot, and only an outcome comparison separates them.

    Args ``lmer`` would refuse outright are :data:`ARGS_UNPARSEABLE`, using the
    CLI's own rules rather than a second opinion about them: a slot backed by
    such a preset would grant a spawn that dies at launch with nothing on the
    slot to explain why.
    """
    args = [str(token) for token in (getattr(preset, "args", None) or [])]
    if not args:
        return None
    key = (
        getattr(preset, "checkout", None),
        getattr(preset, "service", None),
        getattr(preset, "service_group", None),
        tuple(args),
    )
    with _PROBE_LOCK:
        if key in _REBIND_CACHE:
            return _REBIND_CACHE[key]

    answer = _binding_fault(preset, args)
    with _PROBE_LOCK:
        _REBIND_CACHE[key] = answer
    return answer


def _binding_fault(preset: object, args: list) -> Optional[str]:
    """The uncached body of :func:`_rebinding_arg`.

    Parses through ``quiet=True`` rather than capturing output:
    ``contextlib.redirect_stdout`` swaps a **process-global** stream and is not
    thread-safe, and this runs on ``GET /api/state``, which Starlette serves
    from a threadpool — two overlapping calls could leave ``sys.stdout`` an
    in-memory buffer for the life of the daemon.
    """
    # The CLI's own rules for preset args, in its order, so a slot cannot read
    # usable where `lmer` would exit 2.
    if "--" in args:
        return ARGS_UNPARSEABLE
    try:
        probe, probe_rest = parse_args(list(args), quiet=True)
    except ParseRefused:
        return ARGS_UNPARSEABLE
    if probe_rest:
        return ARGS_UNPARSEABLE
    if probe.task or probe.target:
        return ARGS_UNPARSEABLE

    # ``cli_tokens()`` is the preset's own fields followed by its args — the
    # exact argv prefix ``lmer`` re-parses — so this is the real outcome rather
    # than a reading of the spelling.
    try:
        final, _ = parse_args(list(preset.cli_tokens()), quiet=True)
    except ParseRefused:  # pragma: no cover - the probe above catches these
        return ARGS_UNPARSEABLE
    if final.service != getattr(preset, "service", None):
        return "--service"
    # A group is the whole of what a group slot guards, so args that re-decide
    # it move the slot's binding exactly as --service does (issue #312).
    if final.service_group != getattr(preset, "service_group", None):
        return "--service-group"
    if final.checkout != getattr(preset, "checkout", None):
        return "--checkout"
    return None


def _no_presets_reason(preset_name: str) -> str:
    """Why nothing loaded, distinguishing "not configured" from "broken".

    ``load_presets()`` answers an empty mapping for several causes, and only an
    unset variable is fixed by setting it — so only that one says so. The rest
    name the path and point at the daemon log, where ``load_presets`` already
    recorded which cause it was, rather than asserting one untested.
    """
    configured = (os.environ.get(PRESETS_FILE_ENV) or "").strip()
    if not configured:
        return (
            f"no presets are loaded on this host, so preset {preset_name!r} "
            "cannot be resolved — the daemon's own environment must set "
            f"{PRESETS_FILE_ENV} (it is read where the daemon runs, not where "
            "lmer does)"
        )
    return (
        f"no presets loaded from {configured}, so preset {preset_name!r} "
        "cannot be resolved — the file is missing, unreadable, not a JSON "
        "object, or defines no valid entries (the daemon log says which)"
    )


def _resolve_definition(
    definition: SlotDefinition, presets: dict, claimed: dict, groups: dict
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(service, service_group, unusable_reason)`` for one definition.

    *claimed* maps an already-bound binding key to the slot that bound it, and
    is updated here as definitions resolve in declaration order. *groups* maps a
    compose project to ``(reason, member names)`` as the probe last read it, so
    a group slot reserves its members too — two slots that overlap only through
    a group's membership are otherwise both free at baseline, and the operator
    finds out at the second spawn instead of on the row.
    """
    preset = presets.get(definition.preset)
    if preset is None:
        if not presets:
            # A different fault from a name that is merely absent: blaming the
            # wrong one sends the operator to check the thing that is right.
            return None, None, _no_presets_reason(definition.preset)
        return None, None, (
            f"preset {definition.preset!r} is not defined on this host"
        )

    rebinding = _rebinding_arg(preset)
    if rebinding == ARGS_UNPARSEABLE:
        return None, None, (
            f"preset {definition.preset!r} has args lmer cannot parse, so the "
            "session it would start could not run at all"
        )
    if rebinding is not None:
        # Unusable rather than quietly guarding the wrong container: the binding
        # a slot claims has to be the one the session gets.
        return None, None, (
            f"preset {definition.preset!r} sets {rebinding} in its args (or an "
            "abbreviation of it), which overrides the preset's own value — the "
            "slot cannot guarantee what the session binds to"
        )

    service = getattr(preset, "service", None)
    service_group = getattr(preset, "service_group", None)
    if not service and not service_group:
        # Occupying a slot has to *be* running in service mode, so this would
        # produce a session holding a slot against nothing.
        return None, None, (
            f"preset {definition.preset!r} sets no service, so it cannot "
            "put a session into service mode"
        )

    # A group slot is guarded as the group *and* as each of its members: its
    # `service` only says which member the session starts on, and the session
    # may retarget to any of the others.
    if service_group:
        bindings = ((_GROUP, service_group),) + tuple(
            (_SERVICE, member) for member in (groups.get(service_group) or ())
        )
    else:
        bindings = ((_SERVICE, service),)

    # The resource a slot protects is the *service*, not the name over it: two
    # slots resolving to one service would each read free and each grant. First
    # declaration wins, as with a duplicate name and for the same reason.
    for binding in bindings:
        owner = claimed.get(binding)
        if owner is not None:
            return None, None, (
                f"{_describe(binding)} is already bound by slot {owner!r} — one "
                "service takes one slot, or they would both read free and both "
                "grant"
            )
    for binding in bindings:
        claimed[binding] = definition.name
    return service, service_group, None


def _resolve_all(
    definitions: list, presets: dict, groups: Optional[dict] = None
) -> list[tuple]:
    """``[(definition, service, service_group, unusable_reason), …]``, in order.

    *groups* is ``project -> member names`` for the group presets these slots
    name, read once by the caller (:func:`_group_state`) and passed in so the
    membership behind the reservation rule is the same one occupancy uses.

    Resolution only — no occupancy, and no probing *here*: the service-collision
    rule needs every earlier definition resolved before a later one can be
    judged, so keeping the two apart lets :func:`slot_status` apply the identical
    rule to one slot while probing only that slot's service.

    It does not save the group reads, and since issue #312 it cannot: a later
    slot collides with an earlier *group* slot only through that group's
    members, so :func:`slot_status` resolves every declared group's membership
    (via :func:`_group_state`) before calling this — one query per group project,
    memoised on the poll path and deliberately uncached on the claim path, where
    a spawn into any slot therefore pays a fresh read per group project this
    host declares. Bounded by the number of group *slot definitions*, not by
    sessions or members. Scoping those reads to the projects declared at or
    before the requested slot would be sound; it is not done because the saving
    is a fraction of a small constant and the rule is easier to trust whole.
    """
    groups = groups or {}
    claimed: dict[tuple, str] = {}
    return [
        (definition,) + _resolve_definition(definition, presets, claimed, groups)
        for definition in definitions
    ]


def slot_rows(
    config: PlatformConfig,
    *,
    sessions: Optional[list] = None,
    cached: bool = True,
) -> list[SlotStatus]:
    """Every declared slot with its current state.

    *sessions* lets a caller that has already read the registry pass it in
    rather than paying for a second read; the fleet payload does. It is
    filtered for liveness here regardless (see :func:`_live_entries`).
    """
    definitions = slot_definitions(config)
    if not definitions:
        return []

    # Once for all slots, not once each: both of these are file reads.
    presets = load_presets()
    runtime = _detected_runtime()
    groups = _group_state(definitions, presets, runtime=runtime, cached=cached)
    resolutions = _resolve_all(definitions, presets, _members_only(groups))
    by_name, by_binding = _held(
        sessions, presets, _resolved_bindings(resolutions, groups)
    )

    return [
        _status_for(
            definition, service, group, unusable, by_name, by_binding,
            groups=groups, runtime=runtime, cached=cached,
        )
        for definition, service, group, unusable in resolutions
    ]


def _group_state(
    definitions: list, presets: dict, *, runtime: Optional[str], cached: bool
) -> dict:
    """``project -> (reason, members)`` for every group preset a slot names.

    One read per project per poll, shared by the reservation rule, occupancy and
    the row's own service state — the group probe resolves the membership
    anyway, so reusing its answer costs nothing over the probe these slots would
    have paid for regardless.
    """
    projects = []
    for definition in definitions:
        preset = presets.get(definition.preset)
        project = getattr(preset, "service_group", None) if preset else None
        if project and project not in projects:
            projects.append(project)
    return {
        project: group_membership(project, runtime=runtime, cached=cached)
        for project in projects
    }


def _members_only(groups: dict) -> dict:
    """``project -> members`` out of :func:`_group_state`'s pairs."""
    return {project: members for project, (_, members) in groups.items()}


def _resolved_bindings(resolutions: list, groups: dict) -> dict:
    """``slot name -> its binding keys``, for the legacy-entry inference."""
    return {
        definition.name: SlotStatus(
            definition=definition, service=service, service_group=group,
            members=(groups.get(group) or (None, ()))[1] if group else (),
        ).bindings
        for definition, service, group, _ in resolutions
    }


def _status_for(
    definition: SlotDefinition,
    service: Optional[str],
    service_group: Optional[str],
    unusable: Optional[str],
    by_name: dict,
    by_binding: dict,
    *,
    groups: dict,
    runtime: Optional[str],
    cached: bool,
) -> SlotStatus:
    group_reason, members = (
        groups.get(service_group) or (None, ())
    ) if service_group else (None, ())
    resolved = SlotStatus(
        definition=definition, service=service, service_group=service_group,
        members=members,
    )
    occupants = by_name.get(definition.name, ())
    # Holders of this slot's service that claimed it under another name. The
    # rule this backstops is derived from a file that changes while sessions
    # run; this is measured from the sessions, so a config edit cannot flip a
    # live session out of the way.
    service_occupants = tuple(
        (slot_name, held)
        for binding in resolved.bindings
        for slot_name, held in (by_binding.get(binding) or ())
        if slot_name != definition.name
    )
    contested = tuple(
        binding for binding in resolved.bindings
        if any(
            slot_name != definition.name
            for slot_name, _ in (by_binding.get(binding) or ())
        )
    )

    # Skip the probe where its answer cannot change anything, which is also what
    # keeps a fleet of busy slots from costing a query each. ``service`` is None
    # exactly when ``unusable`` is set, so the last test is the middle one over
    # again; both are spelled out because that pairing is a contract of
    # :func:`_resolve_definition` rather than an invariant here.
    if occupants or service_occupants or unusable is not None or not resolved.bindings:
        return SlotStatus(
            definition=definition,
            service=service,
            service_group=service_group,
            members=members,
            unusable_reason=unusable,
            occupants=occupants,
            service_occupants=service_occupants,
            contested=contested,
        )

    return SlotStatus(
        definition=definition,
        service=service,
        service_group=service_group,
        members=members,
        occupants=(),
        # A group's answer is already in hand: :func:`_group_state` read it for
        # the membership, off the same probe this would have run.
        service_down_reason=group_reason if service_group else _probe(
            resolved.bindings[0], runtime=runtime, cached=cached
        ),
    )


def slot_status(
    config: PlatformConfig,
    name: str,
    *,
    sessions: Optional[list] = None,
    cached: bool = True,
) -> Optional[SlotStatus]:
    """One slot's status, or ``None`` when this host declares no such slot.

    ``None`` is the answer the spawn gate turns into a refusal: a typo'd slot
    name must not produce a session that quietly holds nothing.

    Every definition is resolved — the one-service-one-slot rule is order
    dependent. Only the named slot's *service* is probed, but every **group**
    any declared slot names is (:func:`_group_state`, issue #312): a group's
    membership is what the reservation rule expands and what occupancy compares
    against, so it has to be known for the other definitions too and not only
    for this one. One query per group project, shared with that group row's own
    service state — reused from the memo under ``cached``, and read fresh
    otherwise, so the spawn gate's ``cached=False`` call pays one per group
    project this host declares even when the slot it asks about names no group.
    """
    definitions = slot_definitions(config)
    if not any(definition.name == name for definition in definitions):
        return None

    presets = load_presets()
    runtime = _detected_runtime()
    groups = _group_state(definitions, presets, runtime=runtime, cached=cached)
    resolutions = _resolve_all(definitions, presets, _members_only(groups))
    by_name, by_binding = _held(
        sessions, presets, _resolved_bindings(resolutions, groups)
    )
    for definition, service, group, unusable in resolutions:
        if definition.name != name:
            continue
        return _status_for(
            definition, service, group, unusable, by_name, by_binding,
            groups=groups, runtime=runtime, cached=cached,
        )
    return None
