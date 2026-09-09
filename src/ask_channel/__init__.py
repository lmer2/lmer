"""The orchestrator's ask channel: a session's own question/answer directory.

Why this is a package of its own
--------------------------------
Both ends of this feature need one definition of the format, and they run in
different worlds: :mod:`ask_channel.cli` runs *inside* a session's container as
the ``lmer-ask`` console script, while :mod:`lmer_platform.ask` runs in the host
daemon. Putting the format in either of those would make the other import it —
the container-side CLI importing the orchestrator's package, or the daemon
importing a console script — so it lives here, in a module that depends on
nothing but the standard library.

:mod:`ask_channel.protocol` is that shared definition, and the whole contract: a
directory, three file shapes, and the rules for allocating an id. It is the only
re-export here; the CLI is deliberately not imported at package level, so
importing the format costs nothing (the :mod:`slack_chat` convention).

Since issue #407 the directory is a *projection*: the session's communications
live on the supervisor's bus (:mod:`lmer_cli.comms`), the agent's command is
``lmer-signal`` (:mod:`ask_channel.signal_cli`, over :mod:`ask_channel.bus`),
and ``lmer-ask`` (:mod:`ask_channel.cli`) is a one-release shim over it. The
supervisor keeps writing the directory's shapes for readers that predate the
bus, which is why this format still has a home.
"""

from . import protocol

__all__ = ["protocol"]
