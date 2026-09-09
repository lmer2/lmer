"""Who posted a thread message, and what a connector does about it (#427).

The stamp is composed host-side and read back per transport — a matrix content
key, a Slack registry entry — and :class:`Attribution` is the decision a
connector makes from it: relay another run's post, skip its own, and hold a
message nothing can attribute *yet* rather than drop it.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lmer_cli import thread_posts
from lmer_cli.thread_posts import Attribution, Poster


@pytest.fixture(autouse=True)
def registry_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "thread-posts"))
    return tmp_path / "thread-posts"


# ---------------------------------------------------------------------------
# the value
# ---------------------------------------------------------------------------


class TestPoster:
    def test_a_run_is_named_by_its_label(self):
        assert Poster.for_session("s-1", "chat-mr-288").author == "lmer chat-mr-288"

    def test_a_run_with_no_label_yet_falls_back_to_its_session(self):
        assert Poster.for_session("s-1").author == "lmer session s-1"

    def test_the_platform_is_named_by_the_credential(self):
        assert Poster.for_platform("operator").author == "platform · operator"
        assert Poster.for_platform("uber lmer").author == "platform · uber lmer"

    def test_only_a_session_post_is_ever_our_own(self):
        assert Poster.for_session("s-1", "x").is_session("s-1")
        assert not Poster.for_session("s-1", "x").is_session("s-2")
        assert not Poster.for_platform("operator").is_session("s-1")

    def test_a_session_stamp_without_a_session_id_is_not_a_poster(self):
        """Half-attributed would be worse than unattributed: `is_session` could
        never match it, so the run that made the post would read it back as
        another run's (review of !300)."""
        assert Poster.for_session(None, "x") is None
        assert Poster.for_session("") is None
        assert Poster.from_dict({"origin": "session", "label": "chat-mr-288"}) is None
        assert not Poster.for_session("s-1", "x").is_session(None)

    def test_it_round_trips(self):
        for poster in (
            Poster.for_session("s-1", "chat-mr-288"),
            Poster.for_session("s-1"),
            Poster.for_platform("operator"),
        ):
            assert poster is not None
            assert Poster.from_dict(poster.to_dict()) == poster

    @pytest.mark.parametrize("raw", [None, {}, "session", {"origin": "somebody"}, {"session": "s-1"}])
    def test_an_origin_it_does_not_know_is_not_a_poster(self, raw):
        assert Poster.from_dict(raw) is None


# ---------------------------------------------------------------------------
# matrix: on the event
# ---------------------------------------------------------------------------


class TestMatrixContentKey:
    def test_the_stamp_rides_on_the_content(self):
        poster = Poster.for_session("s-1", "chat-mr-288")
        content = {"msgtype": "m.text", "body": "hi", **thread_posts.matrix_content(poster)}
        assert content[thread_posts.MATRIX_CONTENT_KEY] == {
            "origin": "session", "session": "s-1", "label": "chat-mr-288",
        }
        assert thread_posts.poster_from_matrix(content) == poster

    def test_no_poster_stamps_nothing(self):
        assert thread_posts.matrix_content(None) == {}

    @pytest.mark.parametrize("content", [None, "text", {}, {thread_posts.MATRIX_CONTENT_KEY: "s-1"}])
    def test_an_event_without_the_key_names_nobody(self, content):
        assert thread_posts.poster_from_matrix(content) is None


# ---------------------------------------------------------------------------
# Slack: the host-side registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_what_was_recorded_is_what_is_read_back(self):
        poster = Poster.for_session("s-other", "chat-mr-288")
        assert thread_posts.record("C1", "1700.1", poster)
        assert thread_posts.lookup("C1", "1700.1") == poster

    def test_a_message_nobody_recorded_names_nobody(self):
        thread_posts.record("C1", "1700.1", Poster.for_platform("operator"))
        assert thread_posts.lookup("C1", "1700.9") is None
        assert thread_posts.lookup("C9", "1700.1") is None

    def test_a_slack_message_is_looked_up_by_its_own_ts(self):
        poster = Poster.for_platform("uber lmer")
        thread_posts.record("C1", "1700.1", poster)
        assert thread_posts.poster_from_slack("C1", {"ts": "1700.1"}) == poster
        assert thread_posts.poster_from_slack("C1", {"ts": "1700.2"}) is None
        assert thread_posts.poster_from_slack("C1", {}) is None

    def test_recording_a_ref_twice_keeps_the_last_answer(self):
        thread_posts.record("C1", "ref", Poster.for_session("s-1", "one"))
        thread_posts.record("C1", "ref", Poster.for_session("s-2", "two"))
        assert thread_posts.lookup("C1", "ref") == Poster.for_session("s-2", "two")

    def test_an_entry_older_than_the_replay_window_is_pruned_on_write(self, registry_dir):
        old = Poster.for_session("s-old", "old")
        thread_posts.record("C1", "old", old, now=1_000_000.0)
        thread_posts.record(
            "C1", "new", Poster.for_session("s-new", "new"),
            now=1_000_000.0 + thread_posts.ENTRY_TTL_SECONDS + 1,
        )
        assert thread_posts.lookup("C1", "old") is None
        assert thread_posts.lookup("C1", "new") is not None

    def test_one_conversation_s_file_is_bounded(self):
        for index in range(thread_posts.MAX_ENTRIES + 10):
            thread_posts.record(
                "C1", f"ref-{index}", Poster.for_session("s-1", str(index)),
            )
        path = next((registry_path for registry_path in _files()), None)
        assert path is not None
        assert len(json.loads(path.read_text())) == thread_posts.MAX_ENTRIES
        assert thread_posts.lookup("C1", "ref-0") is None
        assert thread_posts.lookup("C1", f"ref-{thread_posts.MAX_ENTRIES + 9}") is not None

    def test_a_conversation_id_cannot_name_a_file_outside_the_registry(self, registry_dir):
        thread_posts.record("../../escaped", "ref", Poster.for_platform("operator"))
        assert thread_posts.lookup("../../escaped", "ref") is not None
        assert [path.name for path in _files()] == [".._.._escaped.json"]

    def test_a_registry_it_cannot_read_costs_a_lookup_not_a_raise(self, registry_dir):
        thread_posts.record("C1", "ref", Poster.for_platform("operator"))
        path = next(iter(_files()))
        path.write_text("{not json")
        assert thread_posts.lookup("C1", "ref") is None

    def test_two_posters_into_one_channel_both_survive(self):
        """The daemon runs every session's servicer as a thread of itself, so
        the read-modify-write is serialised — without that, one poster writes
        the file the other read and an entry goes missing."""
        start = threading.Barrier(2)

        def poster(name):
            start.wait()
            for index in range(25):
                thread_posts.record(
                    "C1", f"{name}-{index}", Poster.for_session(name, name),
                )

        threads = [threading.Thread(target=poster, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        missing = [
            f"{name}-{index}" for name in ("a", "b") for index in range(25)
            if thread_posts.lookup("C1", f"{name}-{index}") is None
        ]
        assert missing == []

    def test_a_conversation_or_ref_it_has_no_id_for_is_not_recorded(self):
        assert not thread_posts.record("", "ref", Poster.for_platform("x"))
        assert not thread_posts.record("C1", "", Poster.for_platform("x"))


def _files():
    return sorted(Path(thread_posts.REGISTRY_DIR).rglob("*.json"))


# ---------------------------------------------------------------------------
# the connector's decision
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Rig:
    """One connector's attribution over a dict of ref -> stamp."""

    def __init__(self, *, session="s-mine"):
        self.stamps: dict = {}
        self.warnings: list = []
        self.clock = Clock()
        self.session = session
        self.attribution = Attribution(
            poster_of=lambda message: self.stamps.get(message["ref"]),
            ref_of=lambda message: message["ref"],
            session_of=lambda: self.session,
            clock=self.clock,
            warn=self.warnings.append,
        )

    def message(self, ref):
        return {"ref": ref}


@pytest.fixture
def rig():
    return Rig()


class TestAttribution:
    def test_our_own_post_is_skipped(self, rig):
        rig.attribution.wrote("m1")
        assert rig.attribution.decide(rig.message("m1")) == (thread_posts.SKIP, None)

    def test_our_own_session_s_post_through_its_servicer_is_skipped(self, rig):
        rig.stamps["m1"] = Poster.for_session("s-mine", "mine")
        assert rig.attribution.decide(rig.message("m1")) == (thread_posts.SKIP, None)

    def test_another_run_s_post_is_ingested(self, rig):
        poster = Poster.for_session("s-other", "chat-mr-288")
        rig.stamps["m1"] = poster
        assert rig.attribution.decide(rig.message("m1")) == (thread_posts.INGEST, poster)

    def test_a_platform_post_is_ingested(self, rig):
        poster = Poster.for_platform("operator")
        rig.stamps["m1"] = poster
        assert rig.attribution.decide(rig.message("m1")) == (thread_posts.INGEST, poster)

    def test_a_message_nothing_can_attribute_waits_rather_than_going_missing(self, rig):
        assert rig.attribution.decide(rig.message("m1")) == (thread_posts.WAIT, None)
        assert rig.attribution.due() == []
        # The registry entry lands a moment after the poll read the message.
        poster = Poster.for_session("s-other", "chat-mr-288")
        rig.stamps["m1"] = poster
        assert rig.attribution.due() == [({"ref": "m1"}, poster)]
        assert rig.attribution.due() == []

    def test_a_held_message_that_turns_out_to_be_ours_is_dropped_quietly(self, rig):
        rig.attribution.decide(rig.message("m1"))
        rig.stamps["m1"] = Poster.for_session("s-mine", "mine")
        assert rig.attribution.due() == []
        assert rig.warnings == []

    def test_the_grace_ends_and_the_first_drop_says_so(self, rig):
        rig.attribution.decide(rig.message("m1"))
        rig.attribution.decide(rig.message("m2"))
        rig.clock.now = thread_posts.GRACE_SECONDS + 1
        assert rig.attribution.due() == []
        assert len(rig.warnings) == 1
        assert "docs/COMMS.md" in rig.warnings[0]
        assert rig.attribution.due() == []
        assert len(rig.warnings) == 1

    def test_a_run_s_post_waits_until_we_know_which_run_we_are(self, rig):
        rig.session = None
        poster = Poster.for_session("s-other", "chat-mr-288")
        rig.stamps["m1"] = poster
        assert rig.attribution.decide(rig.message("m1")) == (thread_posts.WAIT, None)
        rig.session = "s-mine"
        assert rig.attribution.due() == [({"ref": "m1"}, poster)]

    def test_the_platform_needs_no_such_answer(self, rig):
        rig.session = None
        poster = Poster.for_platform("operator")
        rig.stamps["m1"] = poster
        assert rig.attribution.decide(rig.message("m1")) == (thread_posts.INGEST, poster)

    def test_what_it_remembers_is_bounded(self, rig):
        for index in range(thread_posts.HELD_LIMIT + 20):
            rig.attribution.decide(rig.message(f"m{index}"))
        for index in range(thread_posts.WROTE_LIMIT + 20):
            rig.attribution.wrote(f"own{index}")
        assert len(rig.attribution._held) <= thread_posts.HELD_LIMIT
        assert len(rig.attribution._wrote) <= thread_posts.WROTE_LIMIT
