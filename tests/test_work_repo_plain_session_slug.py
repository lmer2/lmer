"""The §7 plain-session predicate and the legacy derivation it compares to.

``work_repo.run_state.is_legacy_plain_session_slug`` is the ONE rule the
validator check ``record.plain-session-slug``, the ``lmer-schema migrate``
blocker and the ``work migrate`` fold share (run-identity spec §7, astra R1).
It matches ONLY a record whose stored ``slug`` is the obsolete derived
spelling of its own recorded target — never a work-item target, never a
re-slugged record, never a missing key (review S9).
"""

from types import SimpleNamespace

import pytest

from work_repo.run_state import is_legacy_plain_session_slug, legacy_derive_slug

#: ``(taskdef, target)`` -> the pre-A1 spelling.
LEGACY_DERIVATIONS = [
    ("chat", "."),
    ("chat", "./x"),
    ("chat", "/abs/path"),
    ("chat", "https://h/acme/widgets"),
    ("chat", "https://h/acme/widgets.git"),
    ("chat", "git@h:acme/widgets.git"),
    ("masterplan", "."),
]


@pytest.mark.parametrize("taskdef,target", LEGACY_DERIVATIONS)
def test_legacy_derivation_is_the_pre_a1_spelling(taskdef, target):
    # The exact spellings the old derive_slug produced (A1 rewrote it to the
    # bare taskdef for every non-work-item target).
    derived = legacy_derive_slug(taskdef, target)
    assert derived.startswith(f"{taskdef}-")
    assert derived != taskdef


def test_legacy_derivation_table():
    assert legacy_derive_slug("chat", ".") == "chat-."
    assert legacy_derive_slug("chat", "https://h/acme/widgets") == "chat-widgets"
    assert legacy_derive_slug("chat", "https://h/acme/widgets.git") == "chat-widgets.git"
    assert legacy_derive_slug("masterplan", ".") == "masterplan-."
    # A work item's spelling is the current derivation too, but it is not a
    # plain-session leftover (the predicate refuses it below).
    assert legacy_derive_slug("chat", "https://h/acme/widgets/-/issues/12") == "chat-issue-12"
    # Empty target / 'default' derive the bare taskdef.
    assert legacy_derive_slug("release", "") == "release"
    assert legacy_derive_slug("release", None) == "release"
    assert legacy_derive_slug("chat", "a" * 40) == "chat-aaaaaaaaaaaa"


@pytest.mark.parametrize("state", [
    {"taskdef": "chat", "target": "https://matrix.to/#/!r:example.org/$e"},
    {"taskdef": "chat", "target": "https://x.slack.com/archives/C12345678/p1700000000123456"},
    {"taskdef": "chat", "target": "https://h/acme/widgets/-/issues/12"},
    {"taskdef": "chat", "target": "feature/login"},
    {"taskdef": "chat", "target": "abc1234"},
    {"taskdef": "chat", "target": "a" * 40},
])
def test_work_item_rows_are_refused_for_the_target_not_the_spelling(state):
    # Each work-item NON_MATCHES row carries its own legacy spelling, so the
    # refusal comes from the work-item exclusion, never a slug mismatch.
    assert any(
        row.get("slug") == legacy_derive_slug(state["taskdef"], state["target"])
        and row.get("target") == state["target"]
        for row in NON_MATCHES
    )


def _state(**overrides):
    state = {"taskdef": "chat", "target": ".", "slug": "chat-."}
    state.update(overrides)
    return state


MATCHES = [
    {"taskdef": "chat", "target": ".", "slug": "chat-."},
    {"taskdef": "chat", "target": "https://h/acme/widgets", "slug": "chat-widgets"},
    {"taskdef": "chat", "target": "https://h/acme/widgets.git", "slug": "chat-widgets.git"},
    {"taskdef": "masterplan", "target": ".", "slug": "masterplan-."},
]

NON_MATCHES = [
    # A release record with an empty target, before version recording...
    {"taskdef": "release", "target": "", "slug": "release"},
    # ...and after (re-slugged): never touched.
    {"taskdef": "release", "target": "", "slug": "release-v0.10.2",
     "reslugged_from": ["release"]},
    # A release launched with '.' and later re-slugged.
    {"taskdef": "release", "target": ".", "slug": "release-.-v0.10.2"},
    # Work-item targets keep the current derivation and are not leftovers.
    {"taskdef": "chat", "target": "https://h/acme/widgets/-/issues/12",
     "slug": "chat-issue-12"},
    {"taskdef": "chat", "target": "feature/login", "slug": "chat-feature-login"},
    {"taskdef": "chat", "target": "abc1234", "slug": "chat-abc1234"},
    # Slack and Matrix permalinks are `special` work items.
    {"taskdef": "chat", "target": "https://x.slack.com/archives/C12345678/p1700000000123456",
     "slug": "chat-p1700000000123456"},
    {"taskdef": "chat", "target": "https://matrix.to/#/!r:example.org/$e", "slug": "chat-$e"},
    # A full SHA: the legacy derivation truncates it to 12 hex.
    {"taskdef": "chat", "target": "a" * 40, "slug": "chat-aaaaaaaaaaaa"},
    # A slug equal to the taskdef is not a plain-session spelling.
    {"taskdef": "chat", "target": ".", "slug": "chat"},
    # Missing keys and wrong types never match (review S9).
    {},
    {"taskdef": "chat"},
    {"slug": "chat-."},
    {"taskdef": "chat", "target": "."},
    {"target": ".", "slug": "chat-."},
    {"taskdef": "", "target": ".", "slug": "-."},
    {"taskdef": "chat", "target": ".", "slug": ""},
    {"taskdef": 1, "target": ".", "slug": "chat-."},
    {"taskdef": "chat", "target": ".", "slug": 1},
    {"taskdef": "chat", "target": ["."], "slug": "chat-."},
    # `target` absent or null reads as '' and derives the bare taskdef, which
    # is never a plain-session spelling.
    {"taskdef": "chat", "target": None, "slug": "chat-."},
    {"taskdef": "chat", "slug": "chat-."},
]


@pytest.mark.parametrize("state", MATCHES)
def test_predicate_matches_the_obsolete_derived_spelling(state):
    assert is_legacy_plain_session_slug(state) is True


@pytest.mark.parametrize("state", NON_MATCHES)
def test_predicate_never_matches_these(state):
    assert is_legacy_plain_session_slug(state) is False


def test_non_mapping_state_never_matches():
    assert is_legacy_plain_session_slug(None) is False
    assert is_legacy_plain_session_slug([]) is False
    assert is_legacy_plain_session_slug(SimpleNamespace(taskdef="chat")) is False


def test_empty_reslugged_from_does_not_suppress_a_match():
    # `reslugged_from: []` records nothing vacated, so the spelling still wins.
    assert is_legacy_plain_session_slug(_state(reslugged_from=[])) is True
    # A non-list reslugged_from is ignored by vacated_slugs the same way.
    assert is_legacy_plain_session_slug(_state(reslugged_from="chat")) is True
