"""Behavioral connector regressions; original source journals remain available."""
import threading
import time

import pytest

from lmer_cli import thread_posts
from tests.connector_fakes import MatrixRig, ROOM, SlackRig, isolated_connectors

@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_actual_raised_ingestion_retains_whole_held_batch(channel, tmp_path, monkeypatch):
    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    connector = rig.connector
    poster = thread_posts.Poster.for_session("other-session")
    monkeypatch.setattr(connector._posters, "_poster_of", lambda message: None)
    if channel == "slack":
        source = [rig.slack.app_says("held-one"), rig.slack.app_says("held-two")]
        poll = connector._poll_thread
    else:
        connector.connect()
        rig.matrix.app_says("held-one", poster=poster)
        rig.matrix.app_says("held-two", poster=poster)
        source = list(rig.matrix.pending)
        def retained_sync(**kwargs):
            events = source if kwargs.get("since") != "held-batch" else []
            return {"next_batch": "held-batch", "rooms": {"join": {ROOM: {"timeline": {"events": events}}}}}
        monkeypatch.setattr(rig.matrix, "sync", retained_sync)
        poll = connector.sync_once
    try:
        poll()
        assert len(connector._posters._held) == 2
        monkeypatch.setattr(connector._posters, "_poster_of", lambda message: poster)
        attempts = []
        original_post = rig.bus.post
        def fail_before_acceptance(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError("injected before supervisor acceptance")
        with monkeypatch.context() as fault:
            fault.setattr(rig.bus, "post", fail_before_acceptance)
            with pytest.raises(RuntimeError, match="before supervisor acceptance"):
                poll()
        assert len(attempts) == 1
        assert rig.bus.post == original_post
        poll()
        poll()
        accepted = [record.text for record in rig.log.read_since(0)[0] if record.text in ("held-one", "held-two")]
        assert len(source) == 2, "source records must remain available"
        assert accepted == ["held-one", "held-two"]
    finally:
        connector.stop(timeout=0.1)
        rig.bus._session._http.close()
        rig.log.close()



@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_pending_answer_and_known_attribution_survive_reconstruction(channel, tmp_path, monkeypatch):
    factory = SlackRig if channel == "slack" else MatrixRig
    rig = factory(tmp_path)
    original = rig.connector
    remote = rig.slack if channel == "slack" else rig.matrix
    original._inbound_once()
    question = rig.agent("question", "original question")
    original._outbound_once()
    poster = thread_posts.Poster.for_session("peer-session")
    if channel == "slack":
        remote.app_says("saved answer")
    else:
        remote.app_says("saved answer", poster=poster)
    monkeypatch.setattr(original._posters, "_poster_of", lambda message: poster)
    method = original._poll_thread if channel == "slack" else original.sync_once
    def fail(*args, **kwargs):
        raise RuntimeError("supervisor temporarily refused prepared answer")
    try:
        with monkeypatch.context() as failure:
            failure.setattr(rig.bus, "post", fail)
            with pytest.raises(RuntimeError, match="prepared answer"):
                method()
        assert original._pending_inbound
        bound = next(iter(original._pending_inbound.values()))
        assert bound["question"] == question.id
        original.stop(timeout=1, announce=False)
        # The original question is now closed; a freshly computed answer would bind the new one.
        rig.bus.post("answer", "already decided", in_reply_to=question.id, source={"channel": "terminal"})
        newer = rig.agent("question", "new question")
        kwargs = {"slack": remote} if channel == "slack" else {"matrix": remote}
        fresh = type(original)(original.config, bus_client=rig.bus, **kwargs)
        monkeypatch.setattr(fresh._posters, "_poster_of", lambda message: None)
        try:
            fresh._ensure_ready()
            fresh._inbound_once()
            matches = [item for item in rig.log.read_since(0)[0] if item.text == "saved answer"]
            assert len(matches) == 1, [item.to_dict() for item in rig.log.read_since(0)[0]]
            assert matches[0].in_reply_to == question.id
            assert matches[0].source.author == poster.author
            assert rig.log.question(newer.id).open
            assert not fresh._pending_inbound
            assert not fresh._posters._held
        finally:
            fresh.stop(timeout=1, announce=False)
    finally:
        original.stop(timeout=1, announce=False)
        rig.bus._session._http.close()
        rig.log.close()


@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_simultaneous_inbound_and_receipt_bookkeeping_never_persist_torn_context(channel, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = (
        "from pathlib import Path; import sys; "
        "from tests.test_connector_ingestion import _interleaving_child; "
        "_interleaving_child(sys.argv[1], Path(sys.argv[2]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, channel, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(root / "src")},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _interleaving_child(channel, tmp_path):
    from lmer_platform import store
    from slack_chat import registry
    from tests.conftest import strip_lmer_env

    with pytest.MonkeyPatch.context() as patch:
        strip_lmer_env(patch)
        patch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "posts"))
        patch.setattr(registry, "REGISTRY_DIR", str(tmp_path / "claims"))
        patch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
        _exercise_interleaving(channel, tmp_path, patch)


def _exercise_interleaving(channel, tmp_path, monkeypatch):
    import copy
    from lmer_cli import connector_delivery

    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    conn = rig.connector
    conn._inbound_once()
    question = rig.agent("question", "concurrent question")
    remote = rig.slack if channel == "slack" else rig.matrix
    remote.human_says("concurrent answer")
    entered, release, inbound_attempt = threading.Event(), threading.Event(), threading.Event()
    snapshots, errors = [], []
    real_write = connector_delivery.write_json
    def capture(path, value):
        if path.name.startswith("session-"):
            snapshots.append(copy.deepcopy(value))
        return real_write(path, value)
    monkeypatch.setattr(connector_delivery, "write_json", capture)
    wrote = conn._posters.wrote
    def pause(receipt):
        entered.set()
        assert release.wait(20), "test bookkeeping barrier was not released"
        wrote(receipt)
    monkeypatch.setattr(conn._posters, "wrote", pause)
    def run(action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)
    def inbound():
        inbound_attempt.set()
        conn._inbound_once()
    outbound = threading.Thread(target=run, args=(conn._outbound_once,))
    incoming = threading.Thread(target=run, args=(inbound,))
    try:
        outbound.start()
        assert entered.wait(20)
        count = len(snapshots)
        incoming.start()
        assert inbound_attempt.wait(5)
        time.sleep(.05)
        assert len(snapshots) == count, "inbound persisted while receipt bookkeeping held the state lock"
    finally:
        release.set()
        outbound.join(timeout=20)
        if incoming.ident is not None:
            incoming.join(timeout=20)
        assert not outbound.is_alive() and not incoming.is_alive()
        conn.stop(timeout=1, announce=False)
        rig.bus._session._http.close()
        rig.log.close()
    assert errors == []
    for snapshot in snapshots:
        if channel == "matrix":
            transport = snapshot["transport"]
            pending = snapshot.get("pending") or {}
            receipt = pending.get("parts", {}).get("text", {}).get("receipt")
            if receipt and receipt in transport["_seen"]:
                assert transport["_question_events"][question.id] == receipt
    assert any(item.text == "concurrent answer" for item in rig.log.read_since(0)[0])


@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_supervisor_acceptance_followed_by_lost_response_retries_original_answer_once(channel, tmp_path, monkeypatch):
    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    conn = rig.connector
    conn._inbound_once()
    question = rig.agent("question", "answer once")
    remote = rig.slack if channel == "slack" else rig.matrix
    remote.human_says("original answer")
    original_post = rig.bus.post
    accepted = []
    def lose_response(*args, **kwargs):
        result = original_post(*args, **kwargs)
        accepted.append(result["message"]["id"])
        raise RuntimeError("supervisor accepted; response lost")
    try:
        with monkeypatch.context() as fault:
            fault.setattr(rig.bus, "post", lose_response)
            with pytest.raises(RuntimeError, match="response lost"):
                conn._inbound_once()
        assert len(accepted) == 1 and conn._pending_inbound
        newer = rig.agent("question", "later question")
        conn._inbound_once()
        messages = [item for item in rig.log.read_since(0)[0] if item.text == "original answer"]
        assert len(messages) == 1
        assert messages[0].id == accepted[0] and messages[0].in_reply_to == question.id
        assert rig.log.question(newer.id).open
        assert not conn._pending_inbound
    finally:
        conn.stop(timeout=1, announce=False)
        rig.bus._session._http.close()
        rig.log.close()


@pytest.mark.parametrize("channel", ["slack", "matrix"])
@pytest.mark.parametrize("reconstruct", [False, True])
def test_attributed_batch_exceeding_hold_limit_drains(channel, reconstruct, tmp_path, monkeypatch):
    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    connector = rig.connector
    remote = rig.slack if channel == "slack" else rig.matrix
    connector._inbound_once()
    poster = thread_posts.Poster.for_session("peer-session")
    source = []
    for index in range(thread_posts.HELD_LIMIT + 1):
        text = "peer message " + str(index)
        source.append(text)
        if channel == "slack": remote.app_says(text)
        else: remote.app_says(text, poster=poster)
    monkeypatch.setattr(connector._posters, "_poster_of", lambda message: poster)
    poll = connector._poll_thread if channel == "slack" else connector.sync_once
    try:
        if reconstruct:
            def unavailable(*args, **kwargs):
                raise RuntimeError("supervisor unavailable with known backlog")
            with monkeypatch.context() as fault:
                fault.setattr(rig.bus, "post", unavailable)
                with pytest.raises(RuntimeError, match="known backlog"):
                    poll()
            assert len(connector._posters._held) == len(source)
            connector.stop(timeout=1, announce=False)
            kwargs = {"slack": remote} if channel == "slack" else {"matrix": remote}
            connector = type(connector)(connector.config, bus_client=rig.bus, **kwargs)
            monkeypatch.setattr(connector._posters, "_poster_of", lambda message: None)
            poll = connector._poll_thread if channel == "slack" else connector.sync_once
        poll()
        actual = [record.text for record in rig.log.read_since(0)[0] if record.text in source]
        assert actual == source
        assert not connector._posters._held
    finally:
        connector.stop(timeout=1, announce=False)
        rig.bus._session._http.close()
        rig.log.close()
