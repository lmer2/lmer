"""``lmer-matrix-bridge`` — ``run``, ``register`` and ``check``.

Three verbs, and the two that are not ``run`` exist because of how this thing
fails. An appservice that is misconfigured does not crash: it sits in a room
saying nothing, or it never receives a transaction, and the operator's evidence
is an absence. So:

``register``
    Emits the appservice registration the homeserver needs, derived from the
    same config the bridge itself reads. The point is that the two cannot drift
    by hand — a sender localpart or a user namespace typed twice is typed
    differently eventually, and the symptom is a bridge whose messages are
    rejected as coming from a user it does not own. **Secrets are placeholders
    by default**: the registration is a file that lands in an Ansible role's
    repository, and a token pasted into one is a token in version control.
    ``--with-secrets`` prints the real values for an operator piping it into a
    vault, and says on stderr that it did.

``check``
    Every refusal ``run`` can hit, with no side effects: the config, the three
    secrets, the crypto store, the room, the media flag, and whether the
    platform daemon answers. An operator runs this when the room has gone
    quiet, which is exactly when the bridge must not "helpfully" create
    anything.

``run``
    The startup sequence from spec §5 — store, room, thread map, one baseline
    snapshot that posts nothing — and then the two loops: poll ``/api/state``
    for transitions, and answer threaded replies as the homeserver pushes them.

Exit codes: ``0`` for a verb that did what it said, ``1`` for a refusal this
process can describe on its own. Argparse's usage errors stay ``2``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from typing import Optional, Sequence

from lmer_platform.client import PlatformError
from matrix_bridge import client as mxclient
from matrix_bridge import config as mxcfg
from matrix_bridge import inbound as mxin
from matrix_bridge import outbound as mxout
from matrix_bridge.threads import ThreadMap

logger = logging.getLogger("matrix_bridge")

#: Every failure this process can report without asking anything. Argparse's own
#: usage errors still exit 2, as they do everywhere in this project.
EXIT_FAILURE = 1

#: The three things ``check`` can say about a precondition. ``note`` exists
#: because a first start legitimately has no room and no ``url``, and a check
#: that failed on a correct config would teach an operator to ignore it.
OK = "ok  "
NOTE = "note"
FAIL = "FAIL"

#: What ``register`` prints where a secret goes unless ``--with-secrets`` is
#: given. Deliberately not a plausible token: nobody can mistake it for one, and
#: a registration installed with it in place fails loudly at the homeserver
#: rather than authenticating as something unexpected.
SECRET_PLACEHOLDER = "CHANGE-ME-{name}"


def registration(
    config: mxcfg.MatrixConfig, secrets: Optional[mxcfg.Secrets] = None,
) -> str:
    """The appservice registration YAML for *config*.

    Written out rather than composed with a YAML library so the file carries its
    own explanation: the operator (or the Ansible role) reading it is being
    asked to trust three flags whose absence produces silence rather than an
    error, and the reasons belong beside them.

    Three MSCs are involved and they are opted into in **two different places**,
    which is the correction the T9 run brought back from Synapse 1.158.0's own
    source:

    - **MSC2409** (to-device messages reach an appservice) and **MSC3202** (the
      appservice may act for its devices — what makes E2EE work without a
      ``/sync`` loop) are homeserver settings *and* per-registration opt-ins.
      The homeserver flags are inert unless this file says ``receive_ephemeral``
      and ``org.matrix.msc3202``.
    - **MSC4190** (device management without ``/login``) is **only** a
      registration key: ``io.element.msc4190``. There is no
      ``experimental_features`` switch for it — Synapse reads that section with
      ``.get()``, so an invented ``msc4190_enabled`` is silently ignored and the
      deployment looks configured while nothing happens.
    """
    as_token = secrets.as_token if secrets else SECRET_PLACEHOLDER.format(
        name="AS-TOKEN")
    hs_token = secrets.hs_token if secrets else SECRET_PLACEHOLDER.format(
        name="HS-TOKEN")
    url = config.url or f"http://{config.bind_address}:{config.bind_port}"

    return f"""\
# Appservice registration for lmer's Matrix bridge (lmer issue #327).
# Emitted by `lmer-matrix-bridge register` from this daemon's config.json —
# regenerate it rather than editing it, so the two cannot drift.
#
# Install it where the homeserver reads app_service_config_files. Every key
# below and every setting named here was checked against matrix-synapse 1.158.0
# itself, not from memory (!245 review found the previous version naming a
# setting Synapse has never had):
#
#   # homeserver.yaml — synapse/config/experimental.py
#   experimental_features:
#     msc2409_to_device_messages_enabled: true   # to-device events (the room keys)
#     msc3202_transaction_extensions: true       # OTK counts / device lists per transaction
#
#   # homeserver.yaml — synapse/config/repository.py, DEFAULT TRUE since 1.120.
#   # Check it is not turned off rather than assuming it needs turning on. Note
#   # the name: Synapse's own key has no prefix; `matrix_enable_authenticated_media`
#   # is the matrix-docker-ansible-deploy role variable that sets it.
#   enable_authenticated_media: true
#
# There is NO `msc3202_device_masquerading` setting — grep 1.158.0 for it and
# you get nothing. Device masquerading needs no homeserver switch; the
# transaction extensions above and this file's `org.matrix.msc3202` are the
# whole opt-in.
#
# There is NO experimental_features switch for MSC4190 either: it is opted into
# per appservice, by `io.element.msc4190` below (synapse/config/appservice.py),
# and Synapse reads experimental_features with .get() — so an `msc4190_enabled`
# there is silently ignored and leaves a deployment that looks configured and
# does nothing.
#
# The two homeserver flags above are equally inert on their own: each needs its
# opt-in in this file too. Without them the bridge receives nothing and says
# nothing — no error, just silence.
id: lmer-{config.name}
url: {url}
as_token: {as_token}
hs_token: {hs_token}
sender_localpart: lmer-{config.name}
# Exclusive, and derived from the sender name rather than configured: two
# bridges on one homeserver must not claim overlapping namespaces, and this is
# what guarantees it.
namespaces:
  users:
    - exclusive: true
      regex: '{config.user_namespace}'
  aliases: []
  rooms: []
rate_limited: false
# MSC2409: the homeserver pushes to-device messages (the room keys the bridge
# needs) on the same transactions as events.
receive_ephemeral: true
# MSC3202's service-side opt-in; the homeserver flags above are inert without
# it.
org.matrix.msc3202: true
# MSC4190, and this key is the ONLY way to enable it — there is no homeserver
# switch (Synapse 1.158.0, config/appservice.py). Everything else that turns it
# on turns it on for every appservice at once, which is the MAS/MSC3861 slice
# this deployment is not doing.
io.element.msc4190: true
"""


def check(config: mxcfg.MatrixConfig, secrets, client, *, out) -> bool:
    """Report every precondition. Returns whether all of them hold.

    No side effects, on purpose: an operator runs this when something is
    already wrong, and a diagnostic that creates a room, mints a device or
    writes a config is a diagnostic that changes what it was asked to measure.
    """
    findings = [
        (OK, "config", f"matrix.name={config.name} sender={config.sender}"),
    ]

    findings.append(
        (OK, "secrets", "all three present") if secrets is not None else
        (FAIL, "secrets",
         f"missing: set {mxcfg.ENV_AS_CREDENTIAL}, {mxcfg.ENV_HS_CREDENTIAL} "
         f"and {mxcfg.ENV_RECOVERY_KEY}")
    )
    # `url` and `room` are **notes, not failures**: a first start legitimately
    # has neither — the registration falls back to the bind address, and the
    # bridge creates the room and records its id. A check that failed on a
    # correct first-start config would teach an operator to ignore it, which is
    # the one thing a diagnostic must never do.
    findings.append((
        OK if config.url else NOTE, "url",
        config.url or f"unset — the registration will say "
                      f"http://{config.bind_address}:{config.bind_port}",
    ))
    findings.append((
        OK if config.authenticated_media else NOTE, "authenticated media",
        "asserted on in config — uploads are permitted (this is the operator's "
        "assertion about the homeserver, not a measurement)"
        if config.authenticated_media else
        "not asserted — the bridge will attach nothing. Set "
        "`matrix.authenticated_media` in the same change that sets "
        "matrix_enable_authenticated_media at the homeserver.",
    ))
    findings.append((
        OK if config.control_url else NOTE, "control url",
        config.control_url or "unset — messages will carry no link back",
    ))
    findings.append(_reachability(config))
    findings.append((
        OK if config.room_id else NOTE, "room",
        config.room_id or "unset — the bridge creates one on first start and "
                          "records it here",
    ))

    findings.append((
        NOTE, "room encryption",
        "checked against the homeserver by the non-local half"
        if config.room_id else
        "not applicable until a room exists",
    ))

    status = mxclient.inspect_store(client.store_path)
    findings.append((
        OK if (status.readable or not status.present) else FAIL,
        "crypto store",
        f"{status.path}: "
        + ("readable" if status.readable else
           "present but unreadable — the bridge will refuse to start unless "
           "the backup restores" if status.present else
           "absent (a first start mints a device)"),
    ))

    allow = config.allow
    findings.append((
        OK if allow else NOTE, "allowlist",
        f"{len(allow)} mxid(s), "
        f"{sum(1 for caps in allow.values() if 'answer-stopped' in caps)} may "
        f"start a session" if allow else
        "empty — the bridge will announce runs and answer nobody",
    ))

    for level, name, detail in findings:
        print(f"{level}  {name}: {detail}", file=out)
    return not any(level == FAIL for level, _, _ in findings)


#: Addresses that mean "this machine, from this machine" — never a destination
#: another host or a container can dial.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

#: Binds that mean "every interface". A socket here answers on the loopback and
#: on every routable address, which is what makes some otherwise-odd pairs work.
#: Not a destination either: nothing can dial ``0.0.0.0``.
_WILDCARD = {"0.0.0.0", "::", ""}


def _in_a_container() -> bool:
    """Is this process inside a container?

    Both runtimes leave a marker: podman writes ``/run/.containerenv``, docker
    ``/.dockerenv``. Neither is a guarantee of anything else, but their presence
    is enough for the one question this answers — whether a loopback bind can
    reach anybody (!246 review).
    """
    from pathlib import Path

    return Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()


def _reachability(config) -> tuple:
    """Can the homeserver reach the address the registration names?

    The registration's ``url`` and the bridge's ``bind_address``/``bind_port``
    are two halves of one fact and nothing else checks them against each other,
    so the failure is the quiet kind: the homeserver POSTs transactions into a
    closed port and the room stays empty.

    **What this can actually know**, which took two rounds to get right (!245
    review):

    - The first cut answered ``ok`` to a port mismatch, a wrong-interface bind
      and a wildcard bind with no ``url`` — defeating the check where it is most
      trusted.
    - The second cut fixed those by inferring "same address means no
      indirection, so the ports must match" — and that inference is false. **A
      reverse proxy binds a port, not an address**: nginx on ``10.0.0.5:443`` in
      front of a bridge on ``10.0.0.5:29331`` is the ordinary topology, and the
      check called it a failure. A check that refuses to start a correct
      deployment is worse than one that stays quiet, because the operator
      believes it.

    So the rule is now narrow and honest. **FAIL only when the registration
    would carry an address nothing can dial** — a wildcard, which is a bind
    directive rather than a destination. Everything else is either an exact
    match (``ok``) or an indirection this process cannot see from config alone
    (``note``, naming the assumption it rests on). Distinguishing a proxy from a
    typo is not possible here, and pretending otherwise is what produced both
    wrong answers.
    """
    from urllib.parse import urlparse

    bind = config.bind_address
    listening_everywhere = bind in _WILDCARD
    listening_on_loopback = bind in _LOOPBACK

    # The default bind is loopback, which is right for the host install D1
    # specifies and usually wrong in a container — where it reaches nothing
    # outside the network namespace. Usually, not always: a homeserver sharing
    # this namespace (the same pod) reaches a loopback listener perfectly well,
    # and that is a legitimate topology to deploy (!246 review). Config cannot
    # tell the two apart, so this is a note naming the assumption rather than a
    # refusal — the same rule the reverse-proxy case settled on. A check that
    # refuses to start a correct deployment is worse than one that stays quiet.
    if listening_on_loopback and _in_a_container():
        return (
            NOTE, "reachability",
            f"`matrix.bind_address` is {bind} and this process is in a "
            f"container: that reaches the homeserver only if it shares this "
            f"network namespace (the same pod). If it does not — a separate "
            f"pod, another host — set 0.0.0.0 and publish the port, because "
            f"loopback here reaches nothing outside this container.",
        )

    if not config.url:
        if listening_everywhere:
            return (
                FAIL, "reachability",
                f"no `matrix.url`, so the registration would say "
                f"http://{bind}:{config.bind_port} — a wildcard bind is a "
                f"directive, not an address anything can dial. Set "
                f"`matrix.url` to the address the homeserver reaches this "
                f"bridge on.",
            )
        if listening_on_loopback:
            return (
                NOTE, "reachability",
                f"the registration will say http://{bind}:{config.bind_port} — "
                f"reachable only from this host, so a homeserver in a container "
                f"or elsewhere will not reach it",
            )
        return (OK, "reachability",
                f"the registration will say http://{bind}:{config.bind_port}")

    parsed = urlparse(config.url)
    named = (parsed.hostname or "").lower()
    try:
        named_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return (FAIL, "reachability",
                f"`matrix.url` has an unusable port: {config.url}")

    if named in _WILDCARD:
        return (
            FAIL, "reachability",
            f"`matrix.url` is {config.url} — a wildcard is a bind directive, "
            f"not an address the homeserver can dial",
        )

    same_address = named == bind or (listening_everywhere and named not in _LOOPBACK)
    if same_address and named_port == config.bind_port:
        return (OK, "reachability", f"{config.url} → {bind}:{config.bind_port}")
    if listening_everywhere and named in _LOOPBACK and named_port == config.bind_port:
        # A socket on every interface answers on loopback too, so a homeserver
        # on this host reaches it there.
        return (OK, "reachability",
                f"{config.url} → every interface on :{config.bind_port}")

    # Everything below is an indirection: something forwards the address or the
    # port the registration names to the one the bridge listens on. A reverse
    # proxy, a published container port, NAT. None of them is visible from here,
    # so each is reported as the assumption it is rather than as a verdict.
    if named_port != config.bind_port and (named == bind or listening_everywhere):
        return (
            NOTE, "reachability",
            f"{config.url} names port {named_port} and the bridge listens on "
            f"{bind}:{config.bind_port} — fine if something forwards that port "
            f"to this one (a reverse proxy or a published container port does "
            f"exactly that), and silent if nothing does",
        )
    if named in _LOOPBACK:
        return (
            NOTE, "reachability",
            f"the registration says {config.url} but the bridge listens on "
            f"{bind} — the homeserver will try its own loopback, which reaches "
            f"this bridge only if it runs on this host and on that interface",
        )
    if listening_on_loopback:
        return (
            NOTE, "reachability",
            f"the registration says {config.url} and the bridge listens on "
            f"{bind}:{config.bind_port} — transactions arrive only if something "
            f"on this host forwards that address to it. If the homeserver runs "
            f"in a container or on another host, set `matrix.bind_address` to "
            f"an address it can route to.",
        )
    return (
        NOTE, "reachability",
        f"the registration says {config.url} and the bridge listens on "
        f"{bind}:{config.bind_port} — a different address, so this depends on "
        f"something forwarding one to the other",
    )


async def check_remote(config, client, endpoint, *, transport, out) -> bool:
    """The questions only the network can answer.

    Three of them: does the homeserver accept this appservice, is the configured
    room encrypted, and does the platform daemon answer. The first two were
    invisible to this verb while it promised to report "every refusal ``run``
    can hit" (!245 review) — and the first is the single most likely cause of
    the quiet room the verb exists for.

    Split from :func:`check` so the local half runs with no homeserver and no
    daemon at all, which is the state an operator is usually in when they run
    this. The media flag is not asked of the homeserver: it is not a
    client-visible fact, and the local half already reports the operator's
    assertion (!243 review).
    """
    from lmer_platform.client import Call, request

    ok = True

    if client is None:
        # No secrets means no Matrix identity to ask the homeserver anything
        # with — but "does the daemon answer?" needs none, and an operator
        # missing one env var should not lose that answer too.
        print(f"{NOTE}  homeserver: not asked — no secrets to ask it with",
              file=out)
    else:
        try:
            ok = await _check_homeserver(config, client, out=out) and ok
        finally:
            # A diagnostic that exits with an "Unclosed client session" warning
            # trains an operator to ignore its output.
            await client.aclose()

    try:
        response = request(endpoint, Call("GET", "/api/health"), timeout=30.0,
                           transport=transport)
        healthy = response.status_code == 200
        detail = f"{endpoint.base_url} answered {response.status_code}"
    except PlatformError as exc:
        healthy, detail = False, str(exc)
    print(f"{OK if healthy else FAIL}  platform: {detail}", file=out)
    return ok and healthy


async def _check_homeserver(config, client, *, out) -> bool:
    """Does the homeserver know this appservice, and is the room encrypted?"""
    try:
        whoami = await client.homeserver.whoami()
    except Exception as exc:
        good, detail = False, f"the homeserver refused the appservice ({exc})"
    else:
        good = whoami == config.sender
        detail = whoami if good else (
            f"the homeserver says this token is {whoami or 'nobody'}, not "
            f"{config.sender} — check the registration is installed and matches"
        )
    print(f"{OK if good else FAIL}  appservice: {detail}", file=out)

    if not config.room_id:
        return good

    # `ensure_room` refuses to start in a room that is not encrypted, so this
    # verb has to be able to see that before `run` hits it.
    try:
        encrypted = await client.homeserver.room_is_encrypted(config.room_id)
        detail = (
            f"{config.room_id} is encrypted" if encrypted else
            f"{config.room_id} is NOT encrypted — the bridge will refuse to "
            f"start rather than post questions and answers in the clear"
        )
    except Exception as exc:
        encrypted, detail = False, f"could not read the room's state ({exc})"
    print(f"{OK if encrypted else FAIL}  room encryption: {detail}", file=out)
    return good and encrypted


async def serve(
    config: mxcfg.MatrixConfig, secrets: mxcfg.Secrets, *,
    stop: Optional[asyncio.Event] = None,
    hurry: Optional[asyncio.Event] = None,
) -> None:
    """Spec §5's startup sequence, then the two halves, running together.

    The startup sequence lives in :func:`_start_bridge` and is raced against
    the stop event; then the two loops run in one ``TaskGroup`` so either
    one's failure ends the process rather than leaving half a bridge running,
    which is the state hardest to notice.

    ``stop``/``hurry`` are the shutdown seam (issue #349), injectable so a
    test can drive a shutdown without sending the process a real signal.
    SIGTERM and SIGINT set ``stop``; a second signal while draining sets
    ``hurry``, which forfeits the outbound grace. ``add_signal_handler``
    rather than ``signal.signal`` so the handler runs on the loop thread and
    may touch asyncio state — and this process is PID 1 in its container,
    where an uninstalled SIGTERM is not defaulted but *dropped*: without a
    handler every ``podman stop`` was the 10-second grace and then SIGKILL.
    """
    stop = stop if stop is not None else asyncio.Event()
    hurry = hurry if hurry is not None else asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(signame: str) -> None:
        if stop.is_set():
            # One signal is polite, two is now: skip the drain's grace.
            logger.info("matrix_bridge_stopping signal=%s immediate=1", signame)
            hurry.set()
            return
        logger.info("matrix_bridge_stopping signal=%s", signame)
        stop.set()

    client = mxclient.connect(config, secrets)
    # Installed before the startup sequence, not just before the run loops: a
    # stop request landing while the store opens should still end in a clean
    # exit, not a SIGKILL. After ``connect``, so a refused config never leaves
    # handlers behind on the loop.
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop, sig.name)
        installed.append(sig)
    try:
        # Startup races the stop event rather than merely following the
        # handler install (!247 review): `start()` is homeserver round trips
        # with no timeout of their own, and a homeserver that accepts the
        # connection but does not answer would otherwise hold a recorded stop
        # request hostage past podman's grace — the SIGKILL again, moved to
        # the exact window where a bad deployment has the operator stopping
        # the unit. A stop here aborts the startup and exits 0.
        startup = asyncio.ensure_future(_start_bridge(config, client))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait(
                {startup, stopping}, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stopping.cancel()
        if not startup.done():
            startup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await startup
            return
        outbound = await startup

        logger.info(
            "matrix_bridge_started room=%s poll=%ss listen=%s:%s", client.room_id,
            config.poll_seconds, config.bind_address, config.bind_port,
        )
        try:
            # A TaskGroup, not a gather: when one half fails, gather raises
            # immediately and leaves the sibling running as an orphan — the
            # `finally` below would then close the session and database under
            # an outbound tick still inside its drain grace, which is the
            # announced-but-unrecorded hazard this shutdown exists to close.
            # The group cancels the sibling and does not exit until both
            # halves have actually finished.
            async with asyncio.TaskGroup() as group:
                group.create_task(client.serve_forever(stop))
                group.create_task(
                    poll_forever(outbound, config.poll_seconds, stop, hurry)
                )
        except BaseExceptionGroup as exc_group:
            # Both halves are done; surface the real failure the way the
            # gather used to, so `main()`'s error handling still sees the
            # exception itself rather than a wrapper it does not catch.
            raise exc_group.exceptions[0] from exc_group
    finally:
        # Drain order (issue #349): the listener went down inside
        # ``serve_forever`` (inbound transactions are redelivered, so refusing
        # them is safe), the poll loop finished or gave up its last tick —
        # only now may the session and the database close under them.
        try:
            await client.aclose()
        except Exception:
            # Raising here would turn a requested stop into a crash exit (or
            # mask the failure that got us here); the journal line is the
            # operator's signal either way.
            logger.exception("matrix_bridge_close_failed")
        for sig in installed:
            loop.remove_signal_handler(sig)
        if stop.is_set():
            logger.info("matrix_bridge_stopped")


async def _start_bridge(config: mxcfg.MatrixConfig, client) -> "mxout.Outbound":
    """Spec §5's startup sequence: listener, store, room, wiring, baseline.

    Both halves of the listener ordering finding (!245 review, two
    iterations): the homeserver *pushes* transactions, so without the
    listener the bridge announces runs and can receive nothing — and it has
    to come before the first homeserver call rather than beside the poll
    loop, because ``start()`` calls the homeserver and the transport's
    outbound client used to be ``AppService.intent``, which raises until the
    web server is up. A listener started beside the run loops was started
    too late to be reached at all.

    A coroutine of its own so ``serve`` can race it against the stop event
    and cancel it wholesale (!247 review).
    """
    await client.listen(config.bind_address, config.bind_port)
    await client.start()

    threads = ThreadMap.load(config.state_dir / "threads.json")
    endpoint = mxout.platform_endpoint()
    outbound = mxout.Outbound(config, client, threads, endpoint)
    inbound = mxin.Inbound(config, client, threads, endpoint)

    client.on_event(inbound.handle)
    await outbound.start()
    return outbound


#: Seconds a stop request waits for an in-flight outbound tick before
#: cancelling it — enough for one send round-trip, and comfortably inside
#: podman's 10-second SIGTERM window with the rest of the teardown behind it.
STOP_TICK_GRACE_SECONDS = 5.0


async def poll_forever(
    outbound, poll_seconds: int,
    stop: asyncio.Event, hurry: asyncio.Event, *,
    tick_grace: float = STOP_TICK_GRACE_SECONDS,
) -> None:
    """The outbound half: one tick per interval, until *stop*.

    A stop during the sleep returns at once — no tick starts after stop. A
    stop during a tick lets that tick finish for *tick_grace* seconds before
    cancelling it: the tick's send-then-bind order (``outbound.py``) means a
    hard kill can leave a run announced but unrecorded, so the common case —
    one send already in flight — gets to complete. ``hurry`` (a second
    signal) forfeits the grace.
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            return
        except asyncio.TimeoutError:
            # A stop landing in the same wake-up as the interval expiring
            # still raises TimeoutError (!247 review) — without this check
            # that one tick starts *after* stop, gets the full grace, and can
            # be cancelled mid-send: the exact window the drain closes,
            # reopened on the shutdown path.
            if stop.is_set():
                return

        tick = asyncio.ensure_future(outbound.tick())
        cancelled_by_us = False
        try:
            await _first_of(tick, stop.wait())
            if not tick.done():
                await _first_of(tick, hurry.wait(), timeout=tick_grace)
            if not tick.done():
                tick.cancel()
                cancelled_by_us = True
            try:
                await tick
            except asyncio.CancelledError:
                # Swallow only the cancellation *we* issued — an external
                # cancel of this coroutine (the TaskGroup reaping a failed
                # sibling) must keep propagating even though the tick shows
                # cancelled too.
                if not cancelled_by_us or asyncio.current_task().cancelling():
                    raise
                logger.info(
                    "matrix_outbound_tick_cancelled reason=%s",
                    "hurry" if hurry.is_set() else "stop_grace",
                )
            except Exception:
                # A tick that raises must not end the bridge: the next one may
                # succeed, and a dead bridge answers nothing at all.
                logger.exception("matrix_outbound_tick_failed")
        finally:
            # An external cancellation while parked in a wait above must not
            # orphan the tick against a transport about to close.
            if not tick.done():
                tick.cancel()
                with contextlib.suppress(BaseException):
                    await tick
        if stop.is_set():
            return


async def _first_of(tick: "asyncio.Future", event_wait, *, timeout=None) -> None:
    """Park until *tick* finishes, *event_wait* fires, or *timeout* runs out."""
    waiter = asyncio.ensure_future(event_wait)
    try:
        await asyncio.wait(
            {tick, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        waiter.cancel()


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lmer-matrix-bridge",
        description=(
            "Reach one lmer platform daemon from Matrix: announce runs that "
            "want a human, and answer them from the thread (issue #327)."
        ),
    )
    sub = parser.add_subparsers(dest="verb")

    sub.add_parser(
        "run", help="Open the store and the room, then poll and answer.",
    )
    emit = sub.add_parser(
        "register",
        help=(
            "Print the appservice registration YAML for this daemon's config. "
            "Secrets are placeholders unless --with-secrets."
        ),
    )
    emit.add_argument(
        "--with-secrets", action="store_true",
        help=(
            "Print the real as_token/hs_token from the environment. For piping "
            "into a vault — not for a file in version control."
        ),
    )
    sub.add_parser(
        "check",
        help="Report every precondition, change nothing.",
    ).add_argument(
        "--local", action="store_true",
        help="Skip the checks that need the network (the homeserver and the daemon).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    if not args.verb:
        parser.print_help(sys.stderr)
        return EXIT_FAILURE

    try:
        config = mxcfg.load()
    except mxcfg.MatrixConfigError as exc:
        print(f"lmer-matrix-bridge: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if args.verb == "register":
        secrets = None
        if args.with_secrets:
            try:
                secrets = mxcfg.load_secrets()
            except mxcfg.MatrixConfigError as exc:
                print(f"lmer-matrix-bridge: {exc}", file=sys.stderr)
                return EXIT_FAILURE
            print(
                "lmer-matrix-bridge: this registration carries live tokens — "
                "it belongs in a vault, not in a repository.", file=sys.stderr,
            )
        print(registration(config, secrets), end="")
        return 0

    if args.verb == "check":
        try:
            secrets = mxcfg.load_secrets()
        except mxcfg.MatrixConfigError:
            secrets = None
        client = mxclient.connect(config, secrets) if secrets else None
        ok = check(
            config, secrets,
            client or mxclient.MatrixClient(config, mxclient.Homeserver()),
            out=sys.stdout,
        )
        if not args.local:
            try:
                endpoint = mxout.platform_endpoint()
            except PlatformError as exc:
                print(f"{FAIL}  platform: {exc}", file=sys.stdout)
                ok = False
            else:
                # Without secrets there is no Matrix identity to ask the
                # homeserver anything with, but "does the daemon answer?" needs
                # none — and an operator missing one env var should not lose
                # that answer too (!245 review).
                ok = asyncio.run(check_remote(
                    config, client, endpoint, transport=None, out=sys.stdout,
                )) and ok
        return 0 if ok else EXIT_FAILURE

    try:
        secrets = mxcfg.load_secrets()
    except mxcfg.MatrixConfigError as exc:
        print(f"lmer-matrix-bridge: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        # No KeyboardInterrupt catch: `serve` handles SIGINT itself, so an
        # interactive Ctrl-C and a `systemctl stop` take the same drain path
        # and both exit 0 (issue #349).
        asyncio.run(serve(config, secrets))
    except (mxclient.MatrixClientError, PlatformError) as exc:
        print(f"lmer-matrix-bridge: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())
