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
from lmer_cli.service import ServiceError, resolve_container

from .config import PlatformConfig, _detected_runtime
from .registry import list_sessions

logger = logging.getLogger("lmer_platform.slots")

__all__ = [
    "SLOT_FREE", "SLOT_OCCUPIED", "SLOT_SERVICE_DOWN", "SLOT_MISCONFIGURED",
    "SLOT_STATES", "PROBE_TTL_SECONDS",
    "SlotDefinition", "SlotStatus",
    "parse_slots", "slot_definitions", "slot_status", "slot_rows",
    "probe_service", "clear_probe_cache",
]

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
    #: The service the slot's preset targets, or ``None`` when it has none.
    service: Optional[str] = None
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
            f"service {self.service!r} is already in use by {who} — one service "
            "takes one slot"
        )

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

#: ``(runtime, service) -> (monotonic deadline, reason or None)``. Guarded by
#: :data:`_PROBE_LOCK`: the daemon serves its routes from a threadpool, so two
#: polls can overlap.
_PROBE_CACHE: dict[tuple[str, str], tuple[float, Optional[str]]] = {}
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
    if runtime is None:
        runtime = _detected_runtime()
    if runtime is None:
        return "no container runtime is available on this host"

    key = (runtime, service)
    if cached:
        with _PROBE_LOCK:
            entry = _PROBE_CACHE.get(key)
        if entry is not None and entry[0] > time.monotonic():
            return entry[1]

    try:
        resolve_container(runtime, service, announce=False)
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
        _PROBE_CACHE[key] = (time.monotonic() + PROBE_TTL_SECONDS, reason)
    return reason


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


def _session_services(
    entry: dict, presets: dict, resolved: dict, name: str
) -> tuple:
    """Every dev service a live session may be bound to, most exact first.

    ``task.slot_service`` is what the gate resolved for this session and is the
    whole answer wherever it exists. A *legacy* entry written before the gate
    recorded it gets two candidates instead — the current service of the preset
    it names, and the slot's current resolution — rather than the first that
    matches: a legacy entry whose slot was repointed would otherwise have its
    block *moved* rather than widened, leaving the service it really holds
    reading free for the gate to grant.

    Not covered: if the *preset* was edited since the session launched, neither
    candidate is the service that session holds and nothing on disk records it.
    The population is entries predating this version, so it drains as they end;
    ``docs/SERVICE-MODE.md`` says so under "Not in this slice".
    """
    task = entry.get("task") if isinstance(entry.get("task"), dict) else {}
    recorded = task.get("slot_service")
    if isinstance(recorded, str) and recorded:
        return (recorded,)

    candidates = []
    preset_name = task.get("preset")
    if isinstance(preset_name, str) and preset_name:
        preset = presets.get(preset_name)
        service = getattr(preset, "service", None) if preset is not None else None
        if service:
            candidates.append(service)
    current = resolved.get(name)
    if current and current not in candidates:
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
    by_service: dict[str, list] = {}
    for entry in _live_entries(sessions):
        name = entry.get("slot")
        if not isinstance(name, str) or not name:
            continue
        occupant = _occupant_of(entry)
        by_name.setdefault(name, []).append(occupant)
        # Possibly more than one for a legacy entry: an inferred service blocks
        # every candidate, so the guess widens the refusal instead of moving it.
        for service in _session_services(entry, presets, resolved, name):
            holders = by_service.setdefault(service, [])
            if occupant not in holders_of(holders):
                holders.append((name, occupant))

    _warn_on_collisions(by_name)
    return (
        {name: tuple(held) for name, held in by_name.items()},
        {service: tuple(held) for service, held in by_service.items()},
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

    ``"--service"``, ``"--checkout"``, :data:`ARGS_UNPARSEABLE`, or ``None``.

    Decided by parsing and comparing **outcomes** rather than matching token
    spellings, because three cases defeat a spelling matcher:

    - ``lmer``'s parser leaves ``allow_abbrev`` on and nothing else shares the
      ``--se``/``--che`` prefixes, so ``--serv`` and ``--che=/x`` rebind exactly
      as the full spelling does.
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
    definition: SlotDefinition, presets: dict, claimed: dict
) -> tuple[Optional[str], Optional[str]]:
    """``(service, unusable_reason)`` for one definition against *presets*.

    *claimed* maps an already-bound service to the slot that bound it, and is
    updated here as definitions resolve in declaration order.
    """
    preset = presets.get(definition.preset)
    if preset is None:
        if not presets:
            # A different fault from a name that is merely absent: blaming the
            # wrong one sends the operator to check the thing that is right.
            return None, _no_presets_reason(definition.preset)
        return None, (
            f"preset {definition.preset!r} is not defined on this host"
        )

    rebinding = _rebinding_arg(preset)
    if rebinding == ARGS_UNPARSEABLE:
        return None, (
            f"preset {definition.preset!r} has args lmer cannot parse, so the "
            "session it would start could not run at all"
        )
    if rebinding is not None:
        # Unusable rather than quietly guarding the wrong container: the binding
        # a slot claims has to be the one the session gets.
        return None, (
            f"preset {definition.preset!r} sets {rebinding} in its args (or an "
            "abbreviation of it), which overrides the preset's own value — the "
            "slot cannot guarantee what the session binds to"
        )

    service = getattr(preset, "service", None)
    if not service:
        # Occupying a slot has to *be* running in service mode, so this would
        # produce a session holding a slot against nothing.
        return None, (
            f"preset {definition.preset!r} sets no service, so it cannot "
            "put a session into service mode"
        )

    # The resource a slot protects is the *service*, not the name over it: two
    # slots resolving to one service would each read free and each grant. First
    # declaration wins, as with a duplicate name and for the same reason.
    owner = claimed.get(service)
    if owner is not None:
        return None, (
            f"service {service!r} is already bound by slot {owner!r} — one "
            "service takes one slot, or they would both read free and both "
            "grant"
        )
    claimed[service] = definition.name
    return service, None


def _resolve_all(
    definitions: list, presets: dict
) -> list[tuple]:
    """``[(definition, service, unusable_reason), …]`` in declaration order.

    Resolution only — no occupancy, no probing. The service-collision rule needs
    every earlier definition resolved before a later one can be judged, while
    the probe is per-slot and expensive, so splitting them lets
    :func:`slot_status` apply the identical rule to one slot without probing the
    rest of the host.
    """
    claimed: dict[str, str] = {}
    return [
        (definition,) + _resolve_definition(definition, presets, claimed)
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
    resolutions = _resolve_all(definitions, presets)
    by_name, by_service = _held(
        sessions, presets,
        {definition.name: service for definition, service, _ in resolutions},
    )
    runtime = _detected_runtime()

    return [
        _status_for(
            definition, service, unusable, by_name, by_service,
            runtime=runtime, cached=cached,
        )
        for definition, service, unusable in resolutions
    ]


def _status_for(
    definition: SlotDefinition,
    service: Optional[str],
    unusable: Optional[str],
    by_name: dict,
    by_service: dict,
    *,
    runtime: Optional[str],
    cached: bool,
) -> SlotStatus:
    occupants = by_name.get(definition.name, ())
    # Holders of this slot's service that claimed it under another name. The
    # rule this backstops is derived from a file that changes while sessions
    # run; this is measured from the sessions, so a config edit cannot flip a
    # live session out of the way.
    service_occupants = tuple(
        (slot_name, held)
        for slot_name, held in (by_service.get(service) or ())
        if slot_name != definition.name
    ) if service else ()

    # Skip the probe where its answer cannot change anything, which is also what
    # keeps a fleet of busy slots from costing a query each. ``service`` is None
    # exactly when ``unusable`` is set, so the last test is the middle one over
    # again; both are spelled out because that pairing is a contract of
    # :func:`_resolve_definition` rather than an invariant here.
    if occupants or service_occupants or unusable is not None or service is None:
        return SlotStatus(
            definition=definition,
            service=service,
            unusable_reason=unusable,
            occupants=occupants,
            service_occupants=service_occupants,
        )

    return SlotStatus(
        definition=definition,
        service=service,
        occupants=(),
        service_down_reason=probe_service(
            service, runtime=runtime, cached=cached
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
    dependent — but only the named slot is probed.
    """
    definitions = slot_definitions(config)
    if not any(definition.name == name for definition in definitions):
        return None

    presets = load_presets()
    resolutions = _resolve_all(definitions, presets)
    by_name, by_service = _held(
        sessions, presets,
        {definition.name: service for definition, service, _ in resolutions},
    )
    runtime = _detected_runtime()
    for definition, service, unusable in resolutions:
        if definition.name != name:
            continue
        return _status_for(
            definition, service, unusable, by_name, by_service,
            runtime=runtime, cached=cached,
        )
    return None
