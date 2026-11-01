"""MR activity: has the author moved since the review was posted? (#401)

The check-in digest's one question about the outside world. What is pinned:

- **Fail open, always.** No token, an HTTP error, a crash in the fetcher, a cache
  that will not write, an entry too old to trust — each reads as "the MR may have
  moved", so the run stays named. The only thing that excludes a run is a fresh,
  successful answer that nothing is newer than the review, or a merged MR.
- **Movement is the author's, not the MR's clock**: a push (a diff version created
  after ``review_posted``) or a comment (a non-system note created after
  ``review_posted`` that the review session did not record as its own write).
  The reviewer's own post-review cleanup — replies it recorded, resolves — and
  GitLab's cross-reference system notes are not movement: they bump
  ``updated_at`` on a review that owes nothing (!282, !198, !346, !352). A time
  is not used to tell whose a comment is: the session's last event can be its
  teardown days later, and an author reply in between would vanish (!149, F3).
- **One fetch per MR per window**, however many runs point at it and however many
  30s ticks pass; fetches run side by side up to a bound, a merged MR is never
  fetched again, and a host that is down is asked once a refresh, not once an MR.

The fetcher is a seam and every test here fakes it; the GitLab adapter is pinned
against a fake ``urlopen``.
"""

import io
import json
import threading
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from lmer_platform import mr_activity, store

WINDOW = 3600
MR = "https://git.example.com/acme/widgets/-/merge_requests/202"
RUN = "git.example.com/acme/widgets/review-mr-202"


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def iso(**delta):
    when = datetime.now(timezone.utc) - timedelta(**delta)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def quiet_mr(**overrides):
    reported = {
        "head_sha": "abc123",
        "pushed_at": iso(hours=7),
        "comments": [[1, iso(hours=7)]],
        "page_floor": None,
        "state": "opened",
    }
    reported.update(overrides)
    return reported


class Fetcher:
    """Counts calls; answers *reported*, or raises it when it is an exception."""

    def __init__(self, reported):
        self.reported = reported
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, ref):
        with self._lock:
            self.calls.append(ref.key)
        if isinstance(self.reported, Exception):
            raise self.reported
        return dict(self.reported)


def age_cache(**delta):
    """Move every cached fetch *delta* into the past."""
    cache = mr_activity.read_cache()
    for entry in cache["mrs"].values():
        entry["fetched_at"] = iso(**delta)
    store.write_json(mr_activity.cache_path(), cache)


def quiet(cache, posted, target=MR, own=()):
    return mr_activity.quiet_since_review(
        cache, target, posted, own_note_ids=own, max_age=2 * WINDOW
    )


def refreshed(reported, *, target=MR):
    return mr_activity.refresh([(RUN, target)], window=WINDOW, fetch=Fetcher(reported))


# --- parsing ---------------------------------------------------------------

def test_a_gitlab_mr_target_parses_into_a_ref():
    ref = mr_activity.parse_mr(MR)
    assert (ref.host, ref.project, ref.iid) == ("git.example.com", "acme/widgets", "202")
    assert ref.key == "git.example.com/acme/widgets!202"
    assert ref.api_url == (
        "https://git.example.com/api/v4/projects/acme%2Fwidgets/merge_requests/202"
    )


@pytest.mark.parametrize("target", [
    None, "", "develop issue 12", "https://github.com/acme/widgets/pull/9",
    "https://git.example.com/acme/widgets/-/issues/4",
])
def test_anything_else_is_not_an_mr(target):
    assert mr_activity.parse_mr(target) is None


# --- the decision ----------------------------------------------------------

def test_a_quiet_mr_is_quiet(platform_root):
    assert quiet(refreshed(quiet_mr()), iso(hours=6)) is True


# Each case is built inside the test, not at collection: an ``iso()`` evaluated
# at import drifts against the test's own ``iso(hours=6)`` on a slow suite.
@pytest.mark.parametrize("moved", [
    pytest.param(lambda: {"pushed_at": iso(minutes=10)}, id="pushed"),
    pytest.param(
        lambda: {"comments": [[2, iso(minutes=10)], [1, iso(hours=7)]]}, id="commented"
    ),
    pytest.param(lambda: {"pushed_at": None}, id="no-push-time"),
    pytest.param(lambda: {"pushed_at": "last tuesday"}, id="unparseable-push"),
    pytest.param(lambda: {"comments": [[2, "last tuesday"]]}, id="unparseable-comment"),
    pytest.param(lambda: {"page_floor": iso(hours=5)}, id="page-does-not-reach-the-review"),
    pytest.param(lambda: {"page_floor": "last tuesday"}, id="unparseable-page-floor"),
])
def test_anything_newer_or_unknown_is_movement(platform_root, moved):
    assert quiet(refreshed(quiet_mr(**moved())), iso(hours=6)) is False


def test_an_mr_with_no_comments_at_all_can_still_be_quiet(platform_root):
    assert quiet(refreshed(quiet_mr(comments=[])), iso(hours=6)) is True


def test_a_full_page_reaching_back_past_the_review_can_be_quiet(platform_root):
    """Only a page whose oldest note is newer than the review hides anything."""
    assert quiet(refreshed(quiet_mr(page_floor=iso(hours=8))), iso(hours=6)) is True


def test_the_mr_clock_alone_is_not_movement(platform_root):
    """!282: the reviewer resolved every thread 11 minutes after its "mergeable"
    round. That bumped ``updated_at`` on the MR and on five notes, and nothing
    else. No push and no new comment means no turn is owed."""
    reported = quiet_mr(updated_at=iso(minutes=5), latest_note_at=iso(minutes=5))
    assert quiet(refreshed(reported), iso(hours=6)) is True


def test_the_reviewers_own_recorded_replies_are_not_movement(platform_root):
    """!352: "verified in <sha>" replies 20-30s after ``review_posted``."""
    replies = quiet_mr(comments=[[9102, iso(hours=5, minutes=59)],
                                 [9101, iso(hours=5, minutes=59)], [1, iso(hours=7)]])
    assert quiet(refreshed(replies), iso(hours=6), own=[9101, 9102]) is True


def test_an_unrecorded_comment_after_the_review_is_movement(platform_root):
    """Whoever wrote it, the review did not record it: fail toward naming."""
    reply = quiet_mr(comments=[[9101, iso(hours=5, minutes=59)], [1, iso(hours=7)]])
    assert quiet(refreshed(reply), iso(hours=6)) is False


def test_an_author_reply_under_the_reviewers_newer_reply_is_still_movement(platform_root):
    """Newest-first: the reviewer answering after the author does not hide the author."""
    both = quiet_mr(comments=[[9105, iso(hours=1)], [700, iso(hours=3)], [1, iso(hours=7)]])
    assert quiet(refreshed(both), iso(hours=6), own=[9105]) is False


def test_the_149_shape_an_author_reply_days_before_the_session_ended(platform_root):
    """F3's regression case, on review-mr-149's real times: review posted
    09-13 01:23:15, the session's only later event its teardown on 09-17
    04:36:43, the author's defence reply 09-14 09:00, no push since the review.
    The reply is not the review's, so the run is owed a turn and is named."""
    posted = "2026-09-13T01:23:15Z"
    reported = quiet_mr(
        pushed_at="2026-09-12T22:10:00Z",
        comments=[[4200, "2026-09-14T09:00:00Z"], [4150, "2026-09-13T01:25:02Z"]],
    )
    cache = refreshed(reported)
    assert quiet(cache, posted, own=[4150]) is False
    age_cache(seconds=WINDOW + 1)
    reviewer_only = refreshed(dict(reported, comments=[[4150, "2026-09-13T01:25:02Z"]]))
    assert quiet(reviewer_only, posted, own=[4150]) is True


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_a_finished_mr_owes_no_review_turn(platform_root, state):
    finished = quiet_mr(state=state, pushed_at=None, comments=[])
    assert quiet(refreshed(finished), iso(hours=6)) is True


def test_a_merged_mr_is_trusted_however_old_its_entry(platform_root):
    """GitLab cannot un-merge an MR, so the answer never goes stale."""
    refreshed(quiet_mr(state="merged"))
    age_cache(days=30)
    assert quiet(mr_activity.read_cache(), iso(hours=6)) is True


def test_a_closed_mr_behind_a_stale_entry_is_not_trusted(platform_root):
    """A closed MR can be reopened."""
    refreshed(quiet_mr(state="closed"))
    age_cache(hours=3)
    assert quiet(mr_activity.read_cache(), iso(hours=6)) is False


def test_no_review_time_is_never_quiet(platform_root):
    cache = refreshed(quiet_mr())
    assert quiet(cache, None) is False
    assert quiet(cache, "not a time") is False


def test_no_cache_is_never_quiet():
    assert quiet(None, iso(hours=6)) is False
    assert quiet({}, iso(hours=6)) is False


def test_an_entry_too_old_to_trust_is_not_quiet(platform_root):
    refreshed(quiet_mr())
    age_cache(hours=3)
    assert quiet(mr_activity.read_cache(), iso(hours=6)) is False


# --- failing open ------------------------------------------------------------

def test_a_fetch_error_is_recorded_and_is_not_quiet(platform_root):
    cache = refreshed(mr_activity.FetchError("HTTP 503"))
    assert quiet(cache, iso(hours=6)) is False
    assert "HTTP 503" in cache["mrs"]["git.example.com/acme/widgets!202"]["error"]


def test_a_crashing_fetcher_is_absorbed(platform_root):
    assert quiet(refreshed(KeyError("sha")), iso(hours=6)) is False


def test_a_merged_error_entry_is_not_trusted(platform_root):
    cache = {"mrs": {"git.example.com/acme/widgets!202": {
        "state": "merged", "error": "HTTP 503", "fetched_at": iso(minutes=1),
    }}}
    assert quiet(cache, iso(hours=6)) is False


def test_a_cache_that_will_not_write_does_not_raise(platform_root, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise store.StoreError("disk full")

    monkeypatch.setattr(mr_activity, "_write_cache", refuse)
    assert quiet(refreshed(quiet_mr()), iso(hours=6)) is True, (
        "this tick's answer is still usable"
    )


def test_a_corrupt_cache_reads_as_empty(platform_root):
    mr_activity.cache_path().parent.mkdir(parents=True, exist_ok=True)
    mr_activity.cache_path().write_text("{not json", encoding="utf-8")
    assert mr_activity.read_cache() == {"mrs": {}}


# --- one fetch per MR per window ------------------------------------------------

def test_two_runs_on_one_mr_cost_one_fetch(platform_root):
    fetch = Fetcher(quiet_mr())
    mr_activity.refresh(
        [(RUN, MR), ("git.example.com/acme/widgets/review-mr-202b", MR)],
        window=WINDOW, fetch=fetch,
    )
    assert fetch.calls == ["git.example.com/acme/widgets!202"]


def test_a_fetch_is_reused_for_the_rest_of_the_window(platform_root):
    fetch = Fetcher(quiet_mr())
    for _tick in range(3):
        mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=fetch)
    assert len(fetch.calls) == 1

    age_cache(seconds=WINDOW + 1)
    mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=fetch)
    assert len(fetch.calls) == 2


def test_a_failed_fetch_is_not_retried_every_tick(platform_root):
    fetch = Fetcher(mr_activity.FetchError("HTTP 503"))
    for _tick in range(3):
        mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=fetch)
    assert len(fetch.calls) == 1


def test_a_merged_mr_is_never_fetched_again(platform_root):
    """135 of 170 live review runs sat on merged MRs, re-read every window (F2)."""
    fetch = Fetcher(quiet_mr(state="merged"))
    mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=fetch)
    age_cache(days=30)
    mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=fetch)
    assert len(fetch.calls) == 1


def test_a_closed_mr_is_fetched_again_each_window(platform_root):
    fetch = Fetcher(quiet_mr(state="closed"))
    mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=fetch)
    age_cache(seconds=WINDOW + 1)
    mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=fetch)
    assert len(fetch.calls) == 2


def test_mrs_no_run_is_waiting_on_are_dropped(platform_root):
    mr_activity.refresh([(RUN, MR)], window=WINDOW, fetch=Fetcher(quiet_mr()))
    assert mr_activity.refresh([], window=WINDOW, fetch=Fetcher(quiet_mr())) == {"mrs": {}}


def test_a_target_that_is_not_an_mr_is_never_fetched(platform_root):
    fetch = Fetcher(quiet_mr())
    mr_activity.refresh(
        [(RUN, "https://github.com/acme/widgets/pull/9")], window=WINDOW, fetch=fetch,
    )
    assert fetch.calls == []


def many(count, host="git.example.com"):
    return [
        (f"{host}/acme/widgets/review-mr-{n}",
         f"https://{host}/acme/widgets/-/merge_requests/{n}")
        for n in range(count)
    ]


def test_fetches_run_side_by_side_up_to_the_bound(platform_root):
    """133s for 170 MRs was one request after another inside the tick (F2).

    Every fetch waits at a barrier sized to the bound: run one at a time, the
    first would time out there and every entry would be an error.
    """
    bound = mr_activity.FETCH_WORKERS
    barrier = threading.Barrier(bound, timeout=10)
    lock = threading.Lock()
    in_flight = {"now": 0, "peak": 0}

    def fetch(ref):
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        try:
            barrier.wait()
        finally:
            with lock:
                in_flight["now"] -= 1
        return quiet_mr()

    cache = mr_activity.refresh(many(bound * 3), window=WINDOW, fetch=fetch)
    assert len(cache["mrs"]) == bound * 3
    assert not [key for key, entry in cache["mrs"].items() if entry.get("error")]
    assert in_flight["peak"] == bound, "never more than the bound at once"


def test_a_host_that_is_down_is_asked_once_a_refresh(platform_root, monkeypatch):
    """A dead host costs one timeout, not one per MR on it."""
    monkeypatch.setattr(mr_activity, "FETCH_WORKERS", 1)
    down = mr_activity.FetchError("URLError reading git.example.com", host_wide=True)
    fetch = Fetcher(down)
    cache = mr_activity.refresh(many(5), window=WINDOW, fetch=fetch)
    assert len(fetch.calls) == 1
    assert len(cache["mrs"]) == 5
    assert all("URLError" in entry["error"] for entry in cache["mrs"].values())


def test_one_mr_failing_does_not_skip_the_rest_of_its_host(platform_root, monkeypatch):
    monkeypatch.setattr(mr_activity, "FETCH_WORKERS", 1)
    fetch = Fetcher(mr_activity.FetchError("HTTP 404"))
    mr_activity.refresh(many(3), window=WINDOW, fetch=fetch)
    assert len(fetch.calls) == 3


def test_another_host_is_still_asked_when_one_is_down(platform_root, monkeypatch):
    monkeypatch.setattr(mr_activity, "FETCH_WORKERS", 1)
    calls = []

    def fetch(ref):
        calls.append(ref.host)
        if ref.host == "down.example.com":
            raise mr_activity.FetchError("timeout", host_wide=True)
        return quiet_mr()

    cache = mr_activity.refresh(
        many(2, "down.example.com") + many(2, "up.example.com"),
        window=WINDOW, fetch=fetch,
    )
    assert calls.count("down.example.com") == 1
    assert calls.count("up.example.com") == 2
    assert quiet(cache, iso(hours=6),
                 target="https://up.example.com/acme/widgets/-/merge_requests/1") is True


# --- the GitLab adapter ------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fake_urlopen(answers, seen):
    def urlopen(request, timeout):
        seen.append((request.full_url, dict(request.header_items()), timeout))
        answer = answers[len(seen) - 1]
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode("utf-8"))

    return urlopen


OPEN_MR = {"sha": "abc123", "updated_at": "2026-09-25T04:50:40.825Z", "state": "opened"}
VERSIONS = [{"created_at": "2026-09-25T03:14:26.520Z", "head_commit_sha": "abc123"}]


def gitlab(answers, seen=None):
    return mr_activity.fetch_gitlab(
        mr_activity.parse_mr(MR), token_for=lambda host: "tkn",
        urlopen=fake_urlopen(answers, [] if seen is None else seen),
    )


def test_the_gitlab_fetch_reads_state_latest_push_and_latest_comment():
    seen = []
    reported = gitlab([OPEN_MR, VERSIONS, [
        {"system": True, "created_at": "2026-09-25T05:00:00.000Z", "body": "resolved all threads"},
        {"system": False, "id": 9102, "created_at": "2026-09-25T04:50:40.199Z",
         "updated_at": "2026-09-25T04:50:40.825Z", "body": "Verified in abc123"},
    ]], seen)
    assert reported == {
        "head_sha": "abc123",
        "state": "opened",
        "pushed_at": "2026-09-25T03:14:26.520Z",
        "comments": [[9102, "2026-09-25T04:50:40.199Z"]],
        "page_floor": None,
    }
    mr_url, headers, timeout = seen[0]
    assert mr_url.endswith("/projects/acme%2Fwidgets/merge_requests/202")
    assert headers["Private-token"] == "tkn"
    assert timeout == mr_activity.FETCH_TIMEOUT_SECONDS
    assert "/versions" in seen[1][0] and "per_page=1" in seen[1][0]
    assert "order_by=created_at" in seen[2][0] and "sort=desc" in seen[2][0]


def test_cross_reference_notes_are_not_comments():
    """!198, !346, !354: the only notes after the review were "mentioned in ..."."""
    reported = gitlab([OPEN_MR, VERSIONS, [
        {"system": True, "created_at": "2026-09-25T01:30:16.207Z", "body": "mentioned in issue #521"},
        {"system": False, "id": 88, "created_at": "2026-09-25T01:28:57.000Z", "body": "review summary"},
    ]])
    assert reported["comments"] == [[88, "2026-09-25T01:28:57.000Z"]]


def test_an_mr_with_no_comments_reports_none():
    reported = gitlab([OPEN_MR, VERSIONS, [
        {"system": True, "created_at": "2026-09-25T01:30:16.207Z", "body": "added 1 commit"},
    ]])
    assert reported["comments"] == []
    assert reported["page_floor"] is None, "the whole history was on the page"


def test_a_full_page_reports_how_far_back_it_reached():
    """Comments older than the page cannot be seen, so the page's reach is kept."""
    page = [{"system": True, "created_at": "2026-09-25T01:00:00Z"}] * (mr_activity.NOTES_PAGE - 1)
    page.append({"system": False, "id": 5, "created_at": "2026-09-20T01:00:00Z"})
    reported = gitlab([OPEN_MR, VERSIONS, page])
    assert reported["page_floor"] == "2026-09-20T01:00:00Z"
    assert reported["comments"] == [[5, "2026-09-20T01:00:00Z"]]


def test_an_mr_with_no_versions_reports_no_push_time():
    assert gitlab([OPEN_MR, [], []])["pushed_at"] is None


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_a_finished_mr_costs_one_request(state):
    seen = []
    reported = gitlab([dict(OPEN_MR, state=state)], seen)
    assert len(seen) == 1
    assert reported["state"] == state


def test_no_token_is_a_host_wide_fetch_error():
    with pytest.raises(mr_activity.FetchError, match="no API token") as caught:
        mr_activity.fetch_gitlab(
            mr_activity.parse_mr(MR), token_for=lambda host: None,
            urlopen=lambda *a, **k: pytest.fail("fetched without a token"),
        )
    assert caught.value.host_wide is True


def test_an_http_error_is_a_fetch_error_that_does_not_carry_the_token():
    error = urllib.error.HTTPError(MR, 503, "Service Unavailable", {}, None)
    with pytest.raises(mr_activity.FetchError) as caught:
        mr_activity.fetch_gitlab(
            mr_activity.parse_mr(MR), token_for=lambda host: "s3cret-tkn",
            urlopen=fake_urlopen([error], []),
        )
    assert "503" in str(caught.value)
    assert "s3cret-tkn" not in str(caught.value)


@pytest.mark.parametrize("error, host_wide", [
    pytest.param(urllib.error.HTTPError(MR, 401, "Unauthorized", {}, None), True, id="401"),
    pytest.param(urllib.error.HTTPError(MR, 403, "Forbidden", {}, None), True, id="403"),
    pytest.param(urllib.error.HTTPError(MR, 404, "Not Found", {}, None), False, id="404"),
    pytest.param(urllib.error.HTTPError(MR, 500, "Server Error", {}, None), False, id="500"),
    pytest.param(urllib.error.URLError("connection refused"), True, id="unreachable"),
    pytest.param(TimeoutError("timed out"), True, id="timeout"),
])
def test_which_failures_speak_for_the_whole_host(error, host_wide):
    with pytest.raises(mr_activity.FetchError) as caught:
        gitlab([error])
    assert caught.value.host_wide is host_wide


def test_a_malformed_answer_is_a_fetch_error():
    with pytest.raises(mr_activity.FetchError):
        gitlab([["not", "an", "mr"]])
