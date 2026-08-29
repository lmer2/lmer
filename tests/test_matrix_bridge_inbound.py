"""``matrix_bridge.inbound`` — who may answer what, and which verb it takes.

G2's claim: *live_question routes only to the session-ask route, question only
to the run-answer route, 429 is reported as "nothing started", unlisted and
under-privileged MXIDs produce no platform call and no room message.*

Both halves are load-bearing and they fail differently:

- **The routing.** ``question`` answers a run that exited, which starts a
  container; ``live_question`` writes into a channel a running session is
  already polling. A message routed to the wrong one either starts a container
  nobody asked for or replies into a channel with no reader.
- **The allowlist.** Both platform credentials open every route, so this is the
  only thing standing between a stranger in a federated room and the fleet. A
  denial must produce *nothing*: no call, and no message telling the sender they
  found something.
"""

import json

import pytest

from lmer_platform import store
from lmer_platform.client import Endpoint
from matrix_bridge import config as mxcfg
from matrix_bridge import inbound as mxin
from matrix_bridge.client import MatrixClient
from matrix_bridge.threads import RunKey, ThreadMap
from tests.conftest import strip_lmer_env
from tests.matrix_fakes import FakeHomeserver

BASE_URL = "http://127.0.0.1:8765"
ROOM = "!room:matrix.example.net"
ROOT = "$thread-root"
ALICE = "@alice:matrix.example.net"
READER = "@reader:matrix.example.net"
STRANGER = "@stranger:matrix.example.net"
RUN = RunKey("gitlab.example.com", "group/project", "develop-327")

STORED = {
    "name": "bridge-a",
    "homeserver": "https://matrix.example.net",
    "room_id": ROOM,
    "allow": {
        ALICE: ["read", "answer-live", "answer-stopped"],
        READER: ["read"],
        "@live-only:matrix.example.net": ["answer-live"],
    },
}


def row(*, reason="question", session_id="s-20260822-aaaa", question_id="1"):
    built = {
        "host": RUN.host, "project": RUN.project, "slug": RUN.slug,
        "title": "Matrix bridge",
        "attention": {"reason": reason, "note": "Which target branch?"},
    }
    if reason == "live_question":
        built["session"] = {"id": session_id, "live": True}
        built["questions"] = [
            {"id": question_id, "text": "Which target branch?"},
            {"id": "2", "text": "and the label?"},
        ]
    return built


def event(sender=ALICE, body="prep-release", *, root=ROOT, msgtype="m.text",
          event_type="m.room.message", relation=True, replying_to=None,
          is_falling_back=None):
    """One inbound event.

    *replying_to* adds the ``m.in_reply_to`` a client sends when the person
    replied to a specific message — the only thing that puts a quoted fallback
    in the body. *is_falling_back* is the spec's marker for a thread reply whose
    ``m.in_reply_to`` is a thread fallback rather than a quote.
    """
    content = {"msgtype": msgtype, "body": body}
    if relation and root:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": root}
        if replying_to:
            content["m.relates_to"]["m.in_reply_to"] = {"event_id": replying_to}
        if is_falling_back is not None:
            content["m.relates_to"]["is_falling_back"] = is_falling_back
    return {"type": event_type, "sender": sender, "content": content}


class Reply:
    def __init__(self, payload=None, status_code=200):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class Platform:
    """Answers ``/api/state`` from a fixed row; records what else is called."""

    def __init__(self, state_row=None, answer=None):
        self.state_row = state_row
        self.answer = answer or Reply({"ok": True})
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers})
        if url.endswith("/api/state"):
            runs = [self.state_row] if self.state_row else []
            return Reply({"runs": runs})
        return self.answer

    @property
    def writes(self):
        """Everything that was not the fleet view — i.e. every side effect."""
        return [c for c in self.calls if not c["url"].endswith("/api/state")]


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return mxcfg.load(STORED)


@pytest.fixture
def homeserver():
    return FakeHomeserver(room_id=ROOM)


@pytest.fixture
def threads(config):
    threads = ThreadMap.load(config.state_dir / "threads.json")
    threads.bind(ROOT, RUN)
    return threads


def build(config, homeserver, threads, platform):
    client = MatrixClient(config, homeserver, record_room_id=lambda _: None)
    return mxin.Inbound(
        config, client, threads, Endpoint(BASE_URL, "secret-value"),
        transport=platform,
    )


# --- parsing an event --------------------------------------------------------

def test_a_threaded_text_message_parses():
    message = mxin.Message.from_event(event())
    assert message.sender == ALICE
    assert message.body == "prep-release"
    assert message.thread_root == ROOT


def test_a_message_outside_a_thread_has_no_root():
    assert mxin.Message.from_event(event(relation=False)).thread_root is None


@pytest.mark.parametrize("event_kwargs", [
    {"event_type": "m.reaction"},
    {"msgtype": "m.image"},
])
def test_what_is_not_a_text_message_is_not_one(event_kwargs):
    assert mxin.Message.from_event(event(**event_kwargs)) is None


def test_a_reply_relation_is_not_a_thread_relation():
    """``m.in_reply_to`` inside a thread relation is how clients mark a reply to
    a specific message; a bare reply with no ``rel_type`` is not threaded, and
    matching it would attach a message to a run nobody addressed."""
    raw = event()
    raw["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": ROOT}}
    assert mxin.Message.from_event(raw).thread_root is None


QUOTED = ("> <@lmer-bridge:matrix.example.net> Which target branch?\n"
          "\nprep-release")


def test_a_rich_replys_quoted_fallback_is_stripped():
    """A client replying to one message prepends the quoted original for
    clients that cannot render the relation. Sending that to the platform would
    answer the question with a copy of the question followed by the answer."""
    raw = event(body=QUOTED, replying_to="$the-question")
    assert mxin.Message.from_event(raw).body == "prep-release"


def test_an_answer_that_merely_begins_with_a_quote_is_left_alone():
    """!245 review: keying the strip on the body's *shape* deleted the first
    line of any answer that legitimately began with a quote, and the person got
    no sign of it — the failure the strip exists to prevent, pointed the other
    way. There is no ``m.in_reply_to`` here, so there is no fallback."""
    raw = event(body=QUOTED)
    assert mxin.Message.from_event(raw).body == QUOTED


def test_a_thread_fallback_is_not_a_quote():
    """A thread reply carries ``m.in_reply_to`` with ``is_falling_back: true``
    to point older clients at the thread root. That is not quoted text, and
    stripping on it would eat a real first line."""
    raw = event(body=QUOTED, replying_to=ROOT, is_falling_back=True)
    assert mxin.Message.from_event(raw).body == QUOTED


def test_a_formatted_reply_block_counts_as_a_fallback():
    """Some clients send the ``<mx-reply>`` block without the relation."""
    raw = event(body=QUOTED, root=ROOT)
    raw["content"]["formatted_body"] = "<mx-reply>…</mx-reply>prep-release"
    assert mxin.Message.from_event(raw).body == "prep-release"


def test_the_persons_own_quoting_further_down_is_theirs_to_keep():
    raw = event(body="prep-release\n\n> because that is where we branch from",
                replying_to="$the-question")
    assert mxin.Message.from_event(raw).body == (
        "prep-release\n\n> because that is where we branch from"
    )


async def test_the_platform_calls_do_not_block_the_event_loop(
    config, homeserver, threads,
):
    """!244 review: ``requests`` blocks, and this coroutine shares its loop with
    the appservice's HTTP server — answering a stopped run starts a container,
    which is not quick."""
    import asyncio
    import threading

    released = threading.Event()

    class Slow(Platform):
        def request(self, method, url, **kwargs):
            if not url.endswith("/api/state"):
                released.wait(timeout=5)
            return super().request(method, url, **kwargs)

    inbound = build(config, homeserver, threads, Slow(row(reason="question")))
    task = asyncio.ensure_future(inbound.handle(event()))
    await asyncio.sleep(0)
    assert not task.done(), "the answer is in flight and the loop is free"
    released.set()
    outcome = await task
    assert outcome.handled is True


# --- routing on the attention reason -----------------------------------------

async def test_inbound_routes_on_attention_reason(config, homeserver, threads):
    """Both directions, which is what makes this a routing test rather than two
    happy paths: neither verb may ever be reachable from the other's state."""
    live = Platform(row(reason="live_question"))
    await build(config, homeserver, threads, live).handle(event())
    assert [c["url"] for c in live.writes] == [
        f"{BASE_URL}/api/sessions/s-20260822-aaaa/ask/1/answer",
    ]
    assert live.writes[0]["json"] == {"answer": "prep-release"}

    stopped = Platform(row(reason="question"))
    await build(config, homeserver, threads, stopped).handle(event())
    assert [c["url"] for c in stopped.writes] == [f"{BASE_URL}/api/runs/answer"]
    assert stopped.writes[0]["json"] == {
        "host": RUN.host, "project": RUN.project, "slug": RUN.slug,
        "answer": "prep-release",
    }


async def test_the_oldest_question_is_the_one_answered(config, homeserver, threads):
    """The session is blocked in the oldest unanswered question — the same one
    the attention note renders, so the room and the route agree."""
    platform = Platform(row(reason="live_question", question_id="7"))
    await build(config, homeserver, threads, platform).handle(event())
    assert platform.writes[0]["url"].endswith("/ask/7/answer")


async def test_principal_header_on_every_call(config, homeserver, threads):
    """G5's named test: one platform credential is shared by every principal the
    bridge speaks for, so the header is what makes the daemon's log say who
    asked. It rides on the write, and never replaces the credential."""
    platform = Platform(row(reason="question"))
    await build(config, homeserver, threads, platform).handle(event())
    headers = platform.writes[0]["headers"]
    assert headers[mxin.PRINCIPAL_HEADER] == ALICE
    assert headers["Authorization"] == "Bearer secret-value"


# --- the allowlist -----------------------------------------------------------

async def test_an_unlisted_sender_produces_no_call_and_no_message(
    config, homeserver, threads,
):
    platform = Platform(row(reason="question"))
    outcome = await build(config, homeserver, threads, platform).handle(
        event(sender=STRANGER),
    )
    assert outcome.handled is False
    assert platform.writes == []
    assert homeserver.sent == [], (
        "a refusal in a federated room tells a stranger they found something"
    )


async def test_a_listed_sender_without_the_capability_is_denied(
    config, homeserver, threads,
):
    platform = Platform(row(reason="question"))
    outcome = await build(config, homeserver, threads, platform).handle(
        event(sender=READER),
    )
    assert outcome.handled is False
    assert platform.writes == []
    assert homeserver.sent == []


async def test_answer_live_does_not_carry_answer_stopped(
    config, homeserver, threads,
):
    """``answer-stopped`` is its own capability because it spawns a container.
    Someone trusted to reply to a running session is not automatically trusted
    to start one."""
    live_only = "@live-only:matrix.example.net"

    permitted = Platform(row(reason="live_question"))
    await build(config, homeserver, threads, permitted).handle(
        event(sender=live_only),
    )
    assert len(permitted.writes) == 1

    refused = Platform(row(reason="question"))
    await build(config, homeserver, threads, refused).handle(
        event(sender=live_only),
    )
    assert refused.writes == []


# --- what the thread is told -------------------------------------------------

@pytest.mark.parametrize("reason, expected", [
    ("live_question", "answered; session continuing"),
    ("question", "answered; session starting"),
])
async def test_the_acknowledgement_says_what_actually_happened(
    config, homeserver, threads, reason, expected,
):
    platform = Platform(row(reason=reason))
    await build(config, homeserver, threads, platform).handle(event())
    assert homeserver.texts == [expected]
    assert homeserver.sent[0].thread_root == ROOT


async def test_inbound_reports_cap_refusal(config, homeserver, threads):
    """The spec's named test: information, never a retry. The cap is the
    operator's to raise, and a bridge that retried would spend the next slot
    that frees up on a message nobody is watching."""
    platform = Platform(row(reason="question"),
                        answer=Reply({"detail": "at the cap"}, status_code=429))
    await build(config, homeserver, threads, platform).handle(event())
    assert homeserver.texts == ["at the concurrency cap; nothing started"]
    assert len(platform.writes) == 1, "no retry"


async def test_another_refusal_is_relayed_in_the_platforms_own_words(
    config, homeserver, threads,
):
    platform = Platform(
        row(reason="question"),
        answer=Reply({"detail": "run develop-327 is not tracked here"},
                     status_code=404),
    )
    await build(config, homeserver, threads, platform).handle(event())
    assert homeserver.texts == ["run develop-327 is not tracked here"]


async def test_a_refusal_with_no_detail_still_says_something(
    config, homeserver, threads,
):
    platform = Platform(row(reason="question"), answer=Reply({}, status_code=500))
    await build(config, homeserver, threads, platform).handle(event())
    assert "500" in homeserver.texts[0]


async def test_an_unreachable_platform_says_nothing_was_answered(
    config, homeserver, threads,
):
    """The person asked and deserves the answer, even when the answer is that
    it did not happen."""
    import requests

    class Down(Platform):
        def request(self, method, url, **kwargs):
            if url.endswith("/api/state"):
                return super().request(method, url, **kwargs)
            raise requests.ConnectionError("connection refused")

    platform = Down(row(reason="question"))
    await build(config, homeserver, threads, platform).handle(event())
    assert "could not reach the platform" in homeserver.texts[0]


# --- everything that is ignored ----------------------------------------------

async def test_a_message_outside_a_thread_is_ignored(config, homeserver, threads):
    platform = Platform(row(reason="question"))
    outcome = await build(config, homeserver, threads, platform).handle(
        event(relation=False),
    )
    assert outcome.handled is False
    assert platform.calls == [] and homeserver.sent == []


async def test_a_thread_the_bridge_does_not_know_is_ignored(
    config, homeserver, threads,
):
    platform = Platform(row(reason="question"))
    outcome = await build(config, homeserver, threads, platform).handle(
        event(root="$some-other-thread"),
    )
    assert outcome.handled is False
    assert platform.calls == [] and homeserver.sent == []


async def test_a_run_with_no_open_question_is_ignored(config, homeserver, threads):
    """A thread stays in the room after its run resolves. A reply in it is not
    an answer to anything, and guessing a verb for it would be an invention."""
    platform = Platform(row(reason="crashed"))
    outcome = await build(config, homeserver, threads, platform).handle(event())
    assert outcome.handled is False
    assert platform.writes == [] and homeserver.sent == []


async def test_a_live_question_with_no_session_is_ignored(
    config, homeserver, threads,
):
    """The row says a session is waiting but carries no session or question id,
    so there is nothing to answer — and no second guess to make."""
    incomplete = row(reason="live_question")
    incomplete.pop("session")
    platform = Platform(incomplete)
    outcome = await build(config, homeserver, threads, platform).handle(event())
    assert outcome.handled is False
    assert platform.writes == []


async def test_a_run_that_is_gone_from_the_fleet_is_ignored(
    config, homeserver, threads,
):
    platform = Platform(None)
    outcome = await build(config, homeserver, threads, platform).handle(event())
    assert outcome.handled is False
    assert platform.writes == []


async def test_the_bridges_own_messages_are_ignored(config, homeserver, threads):
    """They arrive on the same transactions as everyone else's; answering them
    would make one acknowledgement into a loop."""
    platform = Platform(row(reason="question"))
    outcome = await build(config, homeserver, threads, platform).handle(
        event(sender=config.sender),
    )
    assert outcome.handled is False
    assert platform.calls == []


async def test_an_empty_message_is_ignored(config, homeserver, threads):
    platform = Platform(row(reason="question"))
    outcome = await build(config, homeserver, threads, platform).handle(
        event(body="   "),
    )
    assert outcome.handled is False
    assert platform.writes == []
