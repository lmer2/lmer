"""``lmer-matrix-bridge`` — the fleet, reachable from Matrix (issue #327).

One bridge serves one platform daemon (spec D1). It runs on the host where
``~/.lmer/platform/`` is, reads that daemon's secret locally, and speaks to the
same HTTP API every other client speaks to — so it needs no route of its own and
the credential never travels.

What it does, in slice 1: a run that wants a human is announced in one Matrix
room, in a thread of its own (D5, D9); an allowlisted person replies in that
thread and the run continues (D4, D6). Slice 2 drives the session from the same
thread, and everything it needs — the appservice namespace, the per-MXID ×
per-capability allowlist, the client seam, the thread mapping, the principal
header — ships here in the shape slice 2 needs (spec §9).

The modules, and the one thing each is for:

``config``    load and validate ``matrix.*`` from the platform's ``config.json``;
              the three secrets come from the environment and nowhere else
``allow``     ``permits()`` — the only authorization code in the bridge
``threads``   which Matrix thread belongs to which run, persisted
``client``    the single seam over ``mautrix-python``: every homeserver call
``scrub``     the project's secret scrub, applied to what leaves for the room
``outbound``  ``/api/state`` snapshots → transitions → messages
``inbound``   a threaded reply → allow → route → platform call → acknowledgement
``cli``       ``run`` | ``register`` | ``check``

The authorization rule that shapes all of it: both platform credentials open
every route, so the daemon cannot be the boundary here — the bridge is. It
checks an explicit MXID against an explicit capability on every message, and a
sender it does not know is ignored in the room and named in the log.
"""
