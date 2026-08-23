"""``matrix_bridge.allow`` — the only authorization the Matrix feature has.

The daemon says of itself, in its own source, that it is **not** an
authorization boundary: both platform credentials open every route. So a
mistake in this one function is not a defence-in-depth failure, it is *the*
failure — an unlisted MXID answering a run, or spawning a container by
answering a stopped one.

Hence the shape of this file: it tests what does **not** happen. The permit
case is one test; the rest are the ways a sender could be let through by
something other than the operator having written their MXID down.
"""

import pytest

from matrix_bridge import allow as mxallow
from matrix_bridge import config as mxcfg

ALICE = "@alice:matrix.example.net"
PEER = "@lmer-peer:matrix.example.net"

ALLOW = {
    ALICE: frozenset({"read", "answer-live", "answer-stopped"}),
    PEER: frozenset({"read", "answer-live"}),
}


def test_a_listed_mxid_with_the_capability_is_permitted():
    assert mxallow.permits(ALLOW, ALICE, "answer-stopped") is True
    assert mxallow.permits(ALLOW, PEER, "answer-live") is True


def test_allow_denies_unlisted_silently():
    """The spec's named test. Silence is the behaviour: a refusal posted into a
    federated room tells a stranger they found something."""
    assert mxallow.permits(ALLOW, "@stranger:matrix.example.net", "read") is False
    assert mxallow.permits(ALLOW, "@stranger:matrix.example.net",
                           "answer-live") is False


def test_allow_denies_missing_capability():
    """The spec's other named test: listed is not the same as permitted."""
    assert mxallow.permits(ALLOW, PEER, "answer-stopped") is False


def test_a_bot_is_denied_the_same_way_a_human_is():
    """One rule, not two. A peer bridge that is not listed is a stranger."""
    assert mxallow.permits(ALLOW, "@lmer-other:matrix.example.net",
                           "answer-live") is False


def test_the_same_server_is_not_a_permission():
    """Never a domain match. This homeserver federates and all three of its
    identity providers are open-signup, so anyone can hold an MXID on it — the
    deliberate divergence from ``LMER_PUSH_ALLOW_LIST``, which does match
    domains."""
    assert mxallow.permits(ALLOW, "@someone-else:matrix.example.net",
                           "read") is False


def test_a_capability_the_bridge_does_not_implement_is_denied():
    """Slice 2's verbs are refused at config load; if one ever reached here
    through another path, the answer is still no."""
    for capability in mxcfg.RESERVED_CAPABILITIES:
        assert mxallow.permits(ALLOW, ALICE, capability) is False


def test_an_empty_allowlist_permits_nothing():
    """A bridge that only announces: the safe end of the range."""
    assert mxallow.permits({}, ALICE, "read") is False


@pytest.mark.parametrize("mxid", [
    "alice", "@alice", "ALICE:matrix.example.net", "@Alice:matrix.example.net",
    "@alice:matrix.example.net ", " @alice:matrix.example.net",
])
def test_a_near_miss_is_not_a_match(mxid):
    """Exact string equality, including case: Matrix localparts are
    case-sensitive, and a bridge that normalised them would be inventing an
    identity rule the homeserver does not have."""
    assert mxallow.permits(ALLOW, mxid, "read") is False


def test_the_check_reads_the_mapping_it_is_given_every_time():
    """No cache, no module state. An allowlist edited while the bridge runs
    takes effect on the next message — which is what "checked on every message,
    not at join" means in practice."""
    mutable = dict(ALLOW)
    assert mxallow.permits(mutable, "@new:matrix.example.net", "read") is False
    mutable["@new:matrix.example.net"] = frozenset({"read"})
    assert mxallow.permits(mutable, "@new:matrix.example.net", "read") is True


def test_the_module_exposes_exactly_one_function():
    """Authorization spread across helpers is authorization with several ways to
    disagree with itself. A reviewer reads one function or the claim is void."""
    public = [
        name for name in dir(mxallow)
        if not name.startswith("_") and callable(getattr(mxallow, name))
        and getattr(getattr(mxallow, name), "__module__", None) == mxallow.__name__
    ]
    assert public == ["permits"]
    assert mxallow.__all__ == ["permits"]
