"""``matrix_bridge.outbound`` — what the room is told, and how rarely.

G1's claim in full: *a run entering an attention state produces exactly one
thread root carrying title, reason, question text and control-UI link, within
one poll interval; a tick with no change produces nothing.*

The second half is the one that decides whether this feature survives contact
with an operator. A bridge that posts the fleet view every fifteen seconds gets
muted, and a muted room answers no questions — so "produces nothing" is tested
harder here than "produces something".

The platform side is driven through :func:`lmer_platform.client.request`'s
transport seam, and the homeserver side through the fake behind D3's client
seam, so nothing here needs a daemon, a network or a clock.
"""

import asyncio
import json

import pytest

from lmer_platform.client import Endpoint, PlatformError
from lmer_platform import store
from matrix_bridge import config as mxcfg
from matrix_bridge import outbound as mxout
from matrix_bridge.client import MatrixClient
from matrix_bridge.threads import RunKey, ThreadMap
from tests.conftest import strip_lmer_env
from tests.matrix_fakes import FakeHomeserver

BASE_URL = "http://127.0.0.1:8765"
ROOM = "!room:matrix.example.net"
STORED = {
    "name": "bridge-a",
    "homeserver": "https://matrix.example.net",
    "room_id": ROOM,
    "control_url": "https://lmer.example.org",
    "allow": {"@alice:matrix.example.net": ["read", "answer-live"]},
    "remind_seconds": 1800,
}


def row(slug="develop-327", *, reason=None, note=None, title="Matrix bridge",
        since="2026-08-22T12:00:00Z", **extra):
    """One ``/api/state`` run row, with or without attention."""
    built = {
        "host": "gitlab.example.com",
        "project": "group/project",
        "slug": slug,
        "title": title,
        "name": slug,
        "state": "waiting_on_you" if reason else "running",
        "attention": (
            {"reason": reason, "note": note, "since": since, "priority": 0}
            if reason else None
        ),
    }
    built.update(extra)
    return built


class Reply:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class Platform:
    """The daemon as a transport: answers canned snapshots in order."""

    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None,
                timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers})
        payload = self.snapshots.pop(0) if self.snapshots else {"runs": []}
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, Reply):
            return payload
        return Reply(payload)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


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
def threads(platform_root, config):
    return ThreadMap.load(config.state_dir / "threads.json")


@pytest.fixture
def clock():
    return Clock()


def build(config, homeserver, threads, platform, clock):
    client = MatrixClient(config, homeserver, record_room_id=lambda _: None)
    return mxout.Outbound(
        config, client, threads, Endpoint(BASE_URL, "secret-value"),
        transport=platform, clock=clock,
    )


# --- the rules, without any I/O ----------------------------------------------

def test_entering_attention_is_announced_once():
    said, posted = mxout.transitions(
        {}, [row(reason="question")], now=0.0, remind_seconds=1800,
    )
    assert [t.kind for t in said] == [mxout.ANNOUNCE]

    said, posted = mxout.transitions(
        posted, [row(reason="question")], now=1.0, remind_seconds=1800,
    )
    assert said == [], "the second tick changed nothing and said nothing"


def test_a_run_with_no_attention_is_never_mentioned():
    said, posted = mxout.transitions(
        {}, [row(), row("other")], now=0.0, remind_seconds=1800,
    )
    assert said == []
    assert posted == {}


def test_a_reminder_waits_for_remind_seconds():
    said, posted = mxout.transitions(
        {}, [row(reason="question")], now=0.0, remind_seconds=1800,
    )
    for elapsed in (1.0, 900.0, 1799.0):
        said, _ = mxout.transitions(
            posted, [row(reason="question")], now=elapsed, remind_seconds=1800,
        )
        assert said == [], f"a reminder fired {elapsed}s in"

    said, posted = mxout.transitions(
        posted, [row(reason="question")], now=1800.0, remind_seconds=1800,
    )
    assert [t.kind for t in said] == [mxout.REMIND]


def test_a_reminder_does_not_slip_by_a_tick_each_time():
    """The bug this shape invites: refreshing the record on a quiet tick pushes
    the next reminder out forever, and the room goes silent on a run that is
    still waiting."""
    said, posted = mxout.transitions(
        {}, [row(reason="question")], now=0.0, remind_seconds=100,
    )
    for elapsed in range(1, 100):
        said, posted = mxout.transitions(
            posted, [row(reason="question")], now=float(elapsed),
            remind_seconds=100,
        )
        assert said == []
    said, posted = mxout.transitions(
        posted, [row(reason="question")], now=100.0, remind_seconds=100,
    )
    assert [t.kind for t in said] == [mxout.REMIND]


def test_a_changed_reason_is_announced_again():
    """``live_question`` becoming ``question`` is a different situation with a
    different consequence for whoever replies — not a continuation."""
    said, posted = mxout.transitions(
        {}, [row(reason="live_question")], now=0.0, remind_seconds=1800,
    )
    said, posted = mxout.transitions(
        posted, [row(reason="question")], now=1.0, remind_seconds=1800,
    )
    assert [t.kind for t in said] == [mxout.ANNOUNCE]
    assert said[0].attention.reason == "question"


def test_clearing_attention_says_so_once():
    said, posted = mxout.transitions(
        {}, [row(reason="question")], now=0.0, remind_seconds=1800,
    )
    said, posted = mxout.transitions(
        posted, [row()], now=1.0, remind_seconds=1800,
    )
    assert [t.kind for t in said] == [mxout.RESOLVED]

    said, posted = mxout.transitions(
        posted, [row()], now=2.0, remind_seconds=1800,
    )
    assert said == [], "resolved is said once, not on every later tick"


def test_a_run_that_vanishes_from_the_fleet_is_resolved_too():
    """A run that was waiting and is now gone from the snapshot is not still
    waiting; leaving the thread open would leave a question hanging in the room
    that nothing can answer."""
    said, posted = mxout.transitions(
        {}, [row(reason="question")], now=0.0, remind_seconds=1800,
    )
    said, posted = mxout.transitions(posted, [], now=1.0, remind_seconds=1800)
    assert [t.kind for t in said] == [mxout.RESOLVED]
    assert posted == {}


def test_an_unusable_row_is_skipped_rather_than_fatal():
    said, posted = mxout.transitions(
        {}, [{"nonsense": True}, row(reason="question")], now=0.0,
        remind_seconds=1800,
    )
    assert [t.kind for t in said] == [mxout.ANNOUNCE]


# --- what a message says -----------------------------------------------------

def test_an_announcement_carries_the_four_things_and_no_more():
    said, _ = mxout.transitions(
        {}, [row(reason="live_question", note="Which target branch?")],
        now=0.0, remind_seconds=1800,
    )
    text = mxout.compose(said[0], control_url=BASE_URL)
    assert "Matrix bridge" in text, "the run's title"
    assert "waiting on an answer" in text, "the reason in plain words"
    assert "Which target branch?" in text, "the question itself"
    assert BASE_URL in text, "somewhere to go for the rest"
    assert len(text.splitlines()) == 3


def test_the_two_question_reasons_say_what_answering_does():
    """D6's requirement in the message rather than in a comment: one continues a
    session, the other starts a container, and the person tapping reply is
    entitled to know which."""
    live, _ = mxout.transitions(
        {}, [row(reason="live_question")], now=0.0, remind_seconds=1800,
    )
    stopped, _ = mxout.transitions(
        {}, [row(reason="question")], now=0.0, remind_seconds=1800,
    )
    live_text = mxout.compose(live[0], control_url=None)
    stopped_text = mxout.compose(stopped[0], control_url=None)
    assert "the session continues" in live_text
    assert "starts a session" in stopped_text
    assert live_text != stopped_text


def test_a_reminder_reads_as_one():
    said, _ = mxout.transitions(
        {}, [row(reason="question")], now=0.0, remind_seconds=1800,
    )
    reminder = mxout.Transition(mxout.REMIND, said[0].run, said[0].row,
                                said[0].attention)
    assert mxout.compose(reminder, control_url=None).startswith("still waiting")


def test_a_reason_nobody_wrote_words_for_still_reads():
    said, _ = mxout.transitions(
        {}, [row(reason="some_future_reason")], now=0.0, remind_seconds=1800,
    )
    text = mxout.compose(said[0], control_url=None)
    assert "some_future_reason" in text


def test_a_run_with_no_title_falls_back_to_its_name():
    said, _ = mxout.transitions(
        {}, [row(reason="question", title=None)], now=0.0, remind_seconds=1800,
    )
    assert "develop-327" in mxout.compose(said[0], control_url=None)


# --- the link ----------------------------------------------------------------

async def test_the_link_is_the_configured_control_url_not_the_bind_pair(
    config, homeserver, threads, clock,
):
    """!244 review: the endpoint's base URL is where the daemon *listens* —
    ``http://127.0.0.1:8765`` in a chat room costs the reader a tap to discover
    it goes nowhere."""
    platform = Platform({"runs": [row(reason="question")]})
    outbound = build(config, homeserver, threads, platform, clock)
    await outbound.tick()
    assert "https://lmer.example.org" in homeserver.texts[0]
    assert BASE_URL not in homeserver.texts[0]


async def test_no_control_url_means_no_link_rather_than_a_useless_one(
    platform_root, homeserver, threads, clock,
):
    config = mxcfg.load({k: v for k, v in STORED.items() if k != "control_url"})
    platform = Platform({"runs": [row(reason="question", note="Which branch?")]})
    outbound = build(config, homeserver, threads, platform, clock)
    await outbound.tick()
    text = homeserver.texts[0]
    assert "http" not in text
    assert "Which branch?" in text


# --- the loop ----------------------------------------------------------------

async def test_a_waiting_run_opens_exactly_one_thread(
    config, homeserver, threads, clock,
):
    platform = Platform(
        {"runs": [row(reason="question", note="Which target branch?")]},
        {"runs": [row(reason="question", note="Which target branch?")]},
    )
    outbound = build(config, homeserver, threads, platform, clock)

    said = await outbound.tick()
    assert [t.kind for t in said] == [mxout.ANNOUNCE]
    assert len(homeserver.sent) == 1
    assert homeserver.sent[0].thread_root is None, "a root, not a reply"

    clock.advance(15)
    assert await outbound.tick() == []
    assert len(homeserver.sent) == 1


async def test_startup_snapshot_is_baseline(
    config, homeserver, threads, clock,
):
    """The spec's named test: a restart does not re-announce what the room is
    already showing — and "already showing" means *this run has a thread*, which
    is what the baseline now checks."""
    threads.bind("$pre-existing", RunKey("gitlab.example.com", "group/project",
                                         "develop-327"))
    waiting = {"runs": [row(reason="question")]}
    platform = Platform(waiting, waiting)
    outbound = build(config, homeserver, threads, platform, clock)

    await outbound.start()
    assert homeserver.sent == []

    clock.advance(15)
    assert await outbound.tick() == []
    assert homeserver.sent == []


async def test_later_messages_about_a_run_go_into_its_thread(
    config, homeserver, threads, clock,
):
    """D5: one thread per run. The reminder and the resolution are replies, and
    a person scrolling the room sees one conversation per run."""
    waiting = {"runs": [row(reason="question")]}
    platform = Platform(waiting, waiting, {"runs": [row()]})
    outbound = build(config, homeserver, threads, platform, clock)

    await outbound.tick()
    root = homeserver.sent[0]
    clock.advance(config.remind_seconds)
    await outbound.tick()
    clock.advance(15)
    await outbound.tick()

    assert [message.thread_root for message in homeserver.sent] == [
        None, "$event-1", "$event-1",
    ]
    assert root.thread_root is None


async def test_the_thread_survives_a_restart(
    config, homeserver, threads, clock, platform_root,
):
    """The mapping is on disk, so a restarted bridge replies into the thread the
    room is already showing instead of opening a second one."""
    platform = Platform({"runs": [row(reason="question")]})
    outbound = build(config, homeserver, threads, platform, clock)
    await outbound.tick()

    reloaded = ThreadMap.load(config.state_dir / "threads.json")
    assert reloaded.root_for(RunKey("gitlab.example.com", "group/project",
                                    "develop-327")) == "$event-1"


async def test_an_unreachable_platform_posts_nothing_and_logs_once(
    config, homeserver, threads, clock, caplog,
):
    """A daemon that is down is the operator's problem, not the room's — and a
    room told about it every fifteen seconds is a room nobody reads."""
    import requests

    platform = Platform(
        requests.ConnectionError("connection refused"),
        requests.ConnectionError("connection refused"),
        {"runs": [row(reason="question")]},
    )
    outbound = build(config, homeserver, threads, platform, clock)

    with caplog.at_level("WARNING"):
        assert await outbound.tick() == []
        assert await outbound.tick() == []
    assert homeserver.sent == []
    assert sum(
        "matrix_platform_unreachable" in record.message
        for record in caplog.records
    ) == 1

    said = await outbound.tick()
    assert [t.kind for t in said] == [mxout.ANNOUNCE], (
        "a run that started waiting during the outage is still announced when "
        "the platform comes back"
    )


async def test_a_non_200_is_treated_as_unreachable(
    config, homeserver, threads, clock,
):
    platform = Platform(Reply({"detail": "nope"}, status_code=503))
    outbound = build(config, homeserver, threads, platform, clock)
    assert await outbound.tick() == []
    assert homeserver.sent == []


async def test_the_credential_is_a_header_on_the_state_call(
    config, homeserver, threads, clock,
):
    platform = Platform({"runs": []})
    outbound = build(config, homeserver, threads, platform, clock)
    await outbound.tick()
    call = platform.calls[0]
    assert call["headers"] == {"Authorization": "Bearer secret-value"}
    assert "secret-value" not in call["url"]


# --- where the endpoint comes from -------------------------------------------

def test_the_endpoint_is_the_local_daemon_when_nothing_says_otherwise(
    platform_root, monkeypatch,
):
    """The host install: the daemon's own state directory answers both
    questions and the credential never leaves the filesystem."""
    from lmer_platform import config as platform_config

    secret = platform_config.ensure_secret(platform_config.load())
    endpoint = mxout.platform_endpoint()
    assert endpoint.credential == secret
    assert endpoint.base_url == platform_config.load().base_url


def test_the_environment_pair_wins_for_a_containerised_bridge(
    platform_root, monkeypatch,
):
    """Inside a container the daemon's bind pair is *this container's* loopback
    and not a route to anything, so the operator's pair has to win.

    Note what the pair is and is not: worker sessions get no credential at all,
    and the assistant — the one container that does — gets a minted
    per-incarnation credential (#244), not this shared secret. See
    ``platform_endpoint``'s docstring; the trade is the operator's to weigh and
    has not been decided."""
    from lmer_platform import config as platform_config
    from lmer_platform import ctl

    platform_config.ensure_secret(platform_config.load())
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, "http://10.0.0.5:8765")
    monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, "the-daemons-secret")

    endpoint = mxout.platform_endpoint()

    assert endpoint.base_url == "http://10.0.0.5:8765"
    assert endpoint.credential == "the-daemons-secret", (
        "the file is not consulted, so it need not be mounted"
    )


@pytest.mark.parametrize("present, missing", [
    ("ENV_PLATFORM_URL", "ENV_PLATFORM_CREDENTIAL"),
    ("ENV_PLATFORM_CREDENTIAL", "ENV_PLATFORM_URL"),
])
def test_half_the_pair_is_refused_rather_than_ignored(
    platform_root, monkeypatch, present, missing,
):
    """!246 review: swallowing ``CtlError`` collapsed "neither set" and "exactly
    one set" into the same fallback, so a bridge given a URL and no credential
    quietly talked to a *different* daemon than the operator named. The pair is
    all-or-nothing by ctl's own reasoning — a URL with no credential is a 401
    machine, a credential with no URL has nothing to spend itself on."""
    from lmer_platform import config as platform_config
    from lmer_platform import ctl

    platform_config.ensure_secret(platform_config.load())
    monkeypatch.setenv(getattr(ctl, present), "value")
    monkeypatch.delenv(getattr(ctl, missing), raising=False)

    with pytest.raises(PlatformError) as excinfo:
        mxout.platform_endpoint()

    message = str(excinfo.value)
    assert getattr(ctl, missing) in message
    assert getattr(ctl, present) in message


def test_neither_source_is_a_refusal_naming_both(platform_root):
    from lmer_platform import ctl

    with pytest.raises(PlatformError) as excinfo:
        mxout.platform_endpoint()
    message = str(excinfo.value)
    assert ctl.ENV_PLATFORM_URL in message
    assert "secret" in message





# --- what a failed send must not do ------------------------------------------

class Refusing(FakeHomeserver):
    """A homeserver that will not take the first *n* messages."""

    def __init__(self, failures=1, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures

    async def send_text(self, room_id, text, *, thread_root=None):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("the homeserver answered 502")
        return await super().send_text(room_id, text, thread_root=thread_root)


async def test_a_send_that_fails_is_said_again_on_the_next_tick(
    config, threads, clock,
):
    """!244 review: advancing the record before the message lands loses an
    announcement permanently the first time the homeserver 5xxs — the room never
    heard it and the bridge believes it was said."""
    homeserver = Refusing(failures=1, room_id=ROOM)
    waiting = {"runs": [row(reason="question")]}
    platform = Platform(waiting, waiting)
    outbound = build(config, homeserver, threads, platform, clock)

    assert await outbound.tick() == [], "nothing was posted, so nothing is claimed"
    assert homeserver.sent == []

    clock.advance(15)
    said = await outbound.tick()
    assert [t.kind for t in said] == [mxout.ANNOUNCE]
    assert len(homeserver.sent) == 1


async def test_one_runs_failure_does_not_hold_up_the_others(
    config, threads, clock,
):
    class RefusingOne(FakeHomeserver):
        """Refuses the first run's first message, and nothing else."""

        refused = False

        async def send_text(self, room_id, text, *, thread_root=None):
            if "first" in text and not self.refused:
                self.refused = True
                raise RuntimeError("the homeserver answered 502")
            return await super().send_text(room_id, text, thread_root=thread_root)

    homeserver = RefusingOne(room_id=ROOM)
    rows = [row("first", reason="question", title="first"),
            row("second", reason="question", title="second")]
    platform = Platform({"runs": rows}, {"runs": rows})
    outbound = build(config, homeserver, threads, platform, clock)

    said = await outbound.tick()
    assert [t.row["slug"] for t in said] == ["second"]

    clock.advance(15)
    again = await outbound.tick()
    assert [t.row["slug"] for t in again] == ["first"], (
        "the failed run is retried and the successful one is not repeated"
    )


async def test_a_resolution_never_opens_a_thread_of_its_own(
    config, homeserver, threads, clock,
):
    """!244 review: after a restart's baseline, a run that resolves has no
    thread — and opening one whose only message is "resolved" is the *only*
    message that run would ever get."""
    waiting = {"runs": [row(reason="question")]}
    platform = Platform(waiting, {"runs": [row()]})
    outbound = build(config, homeserver, threads, platform, clock)

    await outbound.start()
    clock.advance(15)
    await outbound.tick()

    assert homeserver.sent == []


async def test_a_run_the_room_never_heard_of_is_announced_after_the_baseline(
    config, homeserver, threads, clock,
):
    """!244 review, round 2: the baseline used to record *every* waiting run,
    so a run with no thread was never announced, its reminders were skipped for
    having no thread to go in, and its resolution was skipped too — permanent
    silence about a run that wants a human. That is the first install, and every
    start after a lost or corrupt ``threads.json``.
    """
    waiting = {"runs": [row(reason="question")]}
    platform = Platform(waiting, waiting)
    outbound = build(config, homeserver, threads, platform, clock)

    await outbound.start()
    assert homeserver.sent == [], "the baseline itself still posts nothing"

    clock.advance(15)
    said = await outbound.tick()
    assert [t.kind for t in said] == [mxout.ANNOUNCE]
    assert homeserver.sent[0].thread_root is None, "a thread is opened for it"


async def test_the_baseline_separates_what_the_room_shows_from_what_it_does_not(
    config, homeserver, threads, clock,
):
    """Both runs are waiting; one already has a thread and one does not."""
    threads.bind("$pre-existing", RunKey("gitlab.example.com", "group/project",
                                         "known"))
    rows = [row("known", reason="question", title="known"),
            row("unknown", reason="question", title="unknown")]
    platform = Platform({"runs": rows}, {"runs": rows})
    outbound = build(config, homeserver, threads, platform, clock)

    await outbound.start()
    clock.advance(15)
    said = await outbound.tick()

    assert [t.row["slug"] for t in said] == ["unknown"]


async def test_the_state_read_does_not_block_the_event_loop(
    config, homeserver, threads, clock,
):
    """!244 review: this coroutine shares its loop with the appservice's HTTP
    server, so a blocking 60-second read is 60 seconds in which no inbound
    answer is delivered. The call goes to a thread — which is visible here as
    the loop staying free while it runs."""
    import threading

    ticked = threading.Event()

    class Slow(Platform):
        def request(self, method, url, **kwargs):
            ticked.wait(timeout=5)
            return super().request(method, url, **kwargs)

    outbound = build(config, homeserver, threads, Slow({"runs": []}), clock)
    task = asyncio.ensure_future(outbound.tick())
    await asyncio.sleep(0)
    assert not task.done(), "the read is in flight"
    ticked.set()
    await task
