"""Tests for ``lmer_platform.timetrack_runs`` (issue #665, D21, D22, D26).

The same-run rule across a slug-to-uid key transition, a run's issues from its
target and its metadata, and the facts an automatic entry takes from a run.
"""

import pytest

from lmer_platform import meta, runs, store, timetrack_runs
from lmer_platform.timetrack_runs import RunFacts, canonical_key, facts_for, run_issues, run_keys, same_run
from lmer_timesheet import SessionRef
from tests.conftest import strip_lmer_env

HOST, PROJECT = "git.example.com", "acme/widgets"
SLUG = "develop-issue-12"
UID = "981ec036134f4a8580927ddca71658a9"
SLUG_KEY = f"{HOST}/{PROJECT}/{SLUG}"
UID_KEY = f"{HOST}/{PROJECT}/{UID}"
OTHER_KEY = f"{HOST}/{PROJECT}/develop-issue-13"
ISSUE = "https://git.example.com/acme/widgets/-/issues/12"
MR = "https://git.example.com/acme/widgets/-/merge_requests/7"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _index(entries: dict):
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": entries})


@pytest.fixture
def moved(platform_root):
    """A run first tracked under its slug key, now under its uid key."""
    _index({UID_KEY: {"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID,
                      "target": ISSUE, "aliases": [SLUG_KEY]}})


def test_same_run_by_uid_by_key_and_by_alias(moved):
    assert same_run(SessionRef(uid=UID), SessionRef(run=UID_KEY, uid=UID))
    assert same_run(SessionRef(run=SLUG_KEY), SessionRef(run=SLUG_KEY))
    assert same_run(SessionRef(run=SLUG_KEY), SessionRef(run=UID_KEY, uid=UID))
    assert same_run(SessionRef(run=UID_KEY, uid=UID), SessionRef(run=SLUG_KEY))


def test_two_different_runs_are_not_the_same(moved):
    assert not same_run(SessionRef(run=SLUG_KEY), SessionRef(run=OTHER_KEY))
    assert not same_run(SessionRef(uid=UID), SessionRef(uid="0" * 32))
    assert not same_run(None, SessionRef(run=SLUG_KEY))
    assert not same_run(SessionRef(run=SLUG_KEY), None)


def test_same_run_answers_on_keys_when_the_index_is_unreadable(platform_root, monkeypatch):
    def broken():
        raise OSError("disk")
    monkeypatch.setattr(runs, "load_index", broken)
    assert same_run(SessionRef(run=SLUG_KEY), SessionRef(run=SLUG_KEY))
    assert not same_run(SessionRef(run=SLUG_KEY), SessionRef(run=UID_KEY))


def test_canonical_key_and_run_keys_follow_the_alias(moved):
    assert canonical_key(SessionRef(run=SLUG_KEY)) == UID_KEY
    assert run_keys(SessionRef(run=SLUG_KEY)) == (UID_KEY, SLUG_KEY)
    assert run_keys(SessionRef(uid=UID)) == (UID_KEY, SLUG_KEY)
    assert canonical_key(SessionRef(run=OTHER_KEY)) == OTHER_KEY
    assert run_keys(SessionRef(uid="0" * 32)) == ()


@pytest.mark.parametrize("target, expected", [
    (ISSUE, (ISSUE,)),
    (ISSUE + "?tab=x#note_1", (ISSUE,)),
    ("https://git.example.com/acme/widgets/-/work_items/12", (ISSUE,)),
    (MR, (MR,)),
    ("https://github.com/acme/widgets/pull/9", ("https://github.com/acme/widgets/pull/9",)),
    ("https://github.com/acme/widgets/issues/9", ("https://github.com/acme/widgets/issues/9",)),
    ("develop-issue-12", ()),
    ("issue-12", ()),
    ("https://git.example.com/acme/widgets/-/commit/0123456789abcdef", ()),
    ("https://git.example.com/acme/widgets/-/compare/main...feature", ()),
    ("https://github.com/acme/widgets/commit/0123456789abcdef", ()),
    ("https://git.example.com/acme/widgets", ()),
    (None, ()),
])
def test_run_issues_from_the_target(target, expected):
    assert run_issues(target) == expected


def test_run_issues_put_meta_issues_after_the_target_once_each():
    assert run_issues(ISSUE, [MR, ISSUE + "/", "junk"]) == (ISSUE, MR)
    assert run_issues("develop-issue-12", [MR]) == (MR,)


def test_facts_for_takes_the_title_from_meta_else_the_slug(moved):
    facts = facts_for(SessionRef(run=SLUG_KEY))
    assert facts == RunFacts(ref=SessionRef(run=SLUG_KEY), key=UID_KEY, title=SLUG, issues=(ISSUE,))
    assert facts.work_key == ISSUE
    meta.write(HOST, PROJECT, UID, title="Checkout bug", issues=[MR])
    facts = facts_for(SessionRef(uid=UID))
    assert (facts.title, facts.issues, facts.key) == ("Checkout bug", (ISSUE, MR), UID_KEY)


def test_a_run_with_no_issue_takes_its_key_as_work_key(platform_root):
    runs.track(HOST, PROJECT, SLUG, source="spawned", taskdef="develop", target="issue-12")
    facts = facts_for(SessionRef(run=SLUG_KEY))
    assert facts.issues == () and facts.work_key == SLUG_KEY


def test_facts_for_an_unknown_run_is_none(platform_root):
    assert facts_for(SessionRef(run=OTHER_KEY)) is None
    assert facts_for(None) is None


def test_facts_for_session_reads_the_session_run(moved, monkeypatch):
    monkeypatch.setattr(runs, "run_of_session", lambda session: {
        "host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID} if session == "s-1" else None)
    facts = timetrack_runs.facts_for_session("s-1")
    assert facts.key == UID_KEY and facts.ref == SessionRef(run=UID_KEY, uid=UID)
    assert timetrack_runs.facts_for_session("s-2") is None


def test_facts_for_session_never_raises(platform_root, monkeypatch):
    def broken(session):
        raise RuntimeError("registry")
    monkeypatch.setattr(runs, "run_of_session", broken)
    assert timetrack_runs.facts_for_session("s-1") is None
