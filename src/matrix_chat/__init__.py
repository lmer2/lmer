"""Matrix as a chat channel for lmer sessions (issue #407, spec §5.5).

The mirror of :mod:`slack_chat`: a matrix thread — a room and a thread-root
event, given as a ``matrix.to`` or client permalink — is a session target
(``lmer chat <permalink>``), a connector running in the host ``lmer`` process
mirrors that thread onto the session's bus in both directions
(:mod:`matrix_chat.connector`), and a host-side listener logged in as the
bot account spawns a session when an allowlisted person mentions it
(:mod:`matrix_chat.listener`).

Deliberately separate from :mod:`matrix_bridge`, the fleet-shaped appservice
that announces runs wanting a human into one room per orchestrator. The two
are different shapes and the bridge is left as it is; this package reuses its
scrub and nothing else. Nothing here needs the ``matrix`` extra: a plain
room is the client-server API over HTTP. An encrypted room is refused with
its own name — end-to-end encryption for this connector is a follow-up.
"""
