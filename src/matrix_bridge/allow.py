"""The bridge's authorization, in one function.

Nothing else can hold it. Both platform credentials open every route, and the
daemon says so in its own words — it is not an authorization boundary. So the
question "may this Matrix sender do this thing?" is answered here or nowhere.

The module is one public function on purpose. Authorization spread across a
handful of helpers is authorization with a handful of ways to be inconsistent;
a reviewer should be able to read the whole rule in one screen, and a future
capability should have exactly one place to be added.

The rules, each one a thing this deliberately does not do:

- **Explicit MXIDs only.** Never a server-domain match, never room membership,
  never a power level. This homeserver federates and all three of its identity
  providers are open-signup, so neither the domain nor the room is a trust unit.
  The divergence from ``LMER_PUSH_ALLOW_LIST``'s domain support is deliberate
  and documented so nobody harmonises the two.
- **Humans and bots are the same kind of entry.** A peer bridge is an MXID with
  capabilities. Matrix is an agent bus; treating bots as a separate class would
  mean two authorization rules.
- **Checked per message, not at join.** Membership is not permission, and an
  allowlist edited while the bridge runs must take effect on the next message.
- **A denial is silent in the room.** It is logged, and the sender is told
  nothing: a refusal tells a stranger in a federated room that they found
  something.
"""
from __future__ import annotations

from typing import Mapping

__all__ = ["permits"]


def permits(allow: Mapping[str, frozenset], mxid: str, capability: str) -> bool:
    """May *mxid* exercise *capability*?

    *allow* is :attr:`matrix_bridge.config.MatrixConfig.allow` — already parsed
    and normalised, so this does a membership test and nothing else. Passing it
    in rather than reading module state is what keeps the check per-message and
    the module testable without a config file: there is no cached answer here to
    go stale, and no global to be set in the wrong order at startup.

    Unknown MXID, unknown capability, empty allowlist: all ``False``. There is
    no path through this function that says yes for a reason other than "the
    operator wrote this MXID and this capability next to each other".
    """
    return capability in allow.get(mxid, frozenset())
