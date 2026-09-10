"""Behavioral connector regressions; original source journals remain available."""
import threading
import time

import pytest

from lmer_cli import thread_posts
from tests.connector_fakes import MatrixRig, ROOM, SlackRig, isolated_connectors

@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_confirmed_send_survives_actual_bookkeeping_failure(channel, tmp_path, monkeypatch):
    from slack_chat.client import SlackError
    from matrix_chat.client import MatrixError

    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    connector = rig.connector
    record = rig.agent("message", "keep this source record")
    if channel == "matrix":
        connector.connect()
    remote = rig.slack if channel == "slack" else rig.matrix
    sent = remote.posted if channel == "slack" else remote.sent
    bookkeeping = connector._mirrored if channel == "slack" else connector._posters.wrote
    reached = threading.Event()
    attempts = []
    def fail_once(receipt):
        attempts.append(receipt)
        if len(attempts) == 1:
            reached.set()
            raise (SlackError if channel == "slack" else MatrixError)("bookkeeping failed after confirmation")
        return bookkeeping(receipt)
    if channel == "slack":
        monkeypatch.setattr(connector, "_mirrored", fail_once)
    else:
        monkeypatch.setattr(connector._posters, "wrote", fail_once)
    monkeypatch.setattr(connector, "_backoff", lambda *args: connector._stop.wait(0.005))
    read = rig.bus.messages
    polls = []
    def short_read(cursor, **kwargs):
        polls.append(cursor)
        time.sleep(0.005)
        return read(cursor, timeout=0)
    monkeypatch.setattr(rig.bus, "messages", short_read)
    worker = threading.Thread(target=connector._outbound_loop, daemon=True)
    try:
        worker.start()
        assert reached.wait(5), "actual successful send did not reach bookkeeping fault"
        deadline = time.monotonic() + 3
        while len(polls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(polls) >= 3, "outbound loop must continue after bookkeeping failure"
        assert any(item.id == record.id for item in rig.log.read_since(0)[0])
        assert [item[2] for item in sent].count("keep this source record") == 1
    finally:
        connector._stop.set()
        worker.join(timeout=1)
        connector.stop(timeout=0.1)
        assert not worker.is_alive()
        rig.bus._session._http.close()
        rig.log.close()



@pytest.mark.parametrize("channel", ["slack", "matrix"])
@pytest.mark.parametrize("stage", ["before-replace", "after-replace", "file-sync", "directory-sync", "marker-unlink",
                                  "ledger-before-replace", "ledger-after-replace"])
def test_confirmed_receipt_filesystem_failure_blocks_reconstructed_connector(channel, stage, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from lmer_cli import connector_ownership
    from lmer_cli.connector_delivery import RecoveryRequired, marker_path

    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    conn = rig.connector
    conn._ensure_ready()
    record = rig.agent("question", "retain receipt and question")
    real_replace = connector_ownership.os.replace
    real_sync = connector_ownership.os.fsync
    real_unlink = Path.unlink
    reached = []
    receipt_renamed = []
    def replace(source, destination):
        data = json.loads(Path(source).read_text())
        parts = (data.get("pending") or {}).get("parts", {})
        receipt = (bool(data.get("receipt")) if stage.startswith("ledger-")
                   else any(part.get("receipt") for part in parts.values()))
        position = stage.removeprefix("ledger-")
        if not reached and receipt and position in ("before-replace", "after-replace"):
            reached.append(True)
            if position == "after-replace":
                real_replace(source, destination)
            raise OSError("receipt durability fault at " + stage)
        result = real_replace(source, destination)
        if receipt:
            receipt_renamed.append(True)
        return result
    def sync(fd):
        import os
        import stat
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        fail = stage == "directory-sync" and directory and receipt_renamed
        if stage == "file-sync" and not directory:
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if path.name.startswith("session-"):
                value = json.loads(path.read_text())
                fail = any(part.get("receipt") for part in (value.get("pending") or {}).get("parts", {}).values())
        if not reached and fail:
            reached.append(True)
            raise OSError("receipt durability fault at " + stage)
        return real_sync(fd)
    def unlink(path, *args, **kwargs):
        if not reached and stage == "marker-unlink" and path == marker_path(conn._lease.key):
            reached.append(True)
            raise OSError("marker cleanup fault")
        return real_unlink(path, *args, **kwargs)
    remote = rig.slack if channel == "slack" else rig.matrix
    sent = remote.posted if channel == "slack" else remote.sent
    try:
        with monkeypatch.context() as fault:
            fault.setattr(connector_ownership.os, "replace", replace)
            fault.setattr(connector_ownership.os, "fsync", sync)
            fault.setattr(Path, "unlink", unlink)
            with pytest.raises(RecoveryRequired, match="receipt"):
                conn._outbound_once()
        assert reached and len(sent) == 1
        conn.stop(timeout=1, announce=False)
        assert marker_path(conn._lease.key).exists()
        kwargs = {"slack": remote} if channel == "slack" else {"matrix": remote}
        fresh = type(conn)(conn.config, bus_client=rig.bus, **kwargs)
        try:
            with pytest.raises(RecoveryRequired):
                fresh._ensure_ready()
            assert len(sent) == 1
            assert any(item.id == record.id for item in rig.log.read_since(0)[0])
        finally:
            fresh.stop(timeout=1, announce=False)
    finally:
        conn.stop(timeout=1, announce=False)
        rig.bus._session._http.close()
        rig.log.close()


@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_verified_pre_send_marker_sync_failure_can_retry_without_manual_uncertainty(channel, tmp_path, monkeypatch):
    import os
    from lmer_cli import connector_delivery

    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    conn = rig.connector
    conn._ensure_ready()
    record = rig.agent("message", "verified not sent yet")
    original = connector_delivery.os.fsync
    reached = []
    def fsync(fd):
        if not reached and "/pending-send.json." in os.readlink(f"/proc/self/fd/{fd}"):
            reached.append(True)
            raise OSError("marker fsync failed before send")
        return original(fd)
    sent = rig.slack.posted if channel == "slack" else rig.matrix.sent
    try:
        with monkeypatch.context() as fault:
            fault.setattr(connector_delivery.os, "fsync", fsync)
            with pytest.raises(OSError, match="before send"):
                conn._outbound_once()
        assert reached and sent == []
        assert not conn._state.blocked
        conn._outbound_once()
        assert len(sent) == 1
        assert any(item.id == record.id for item in rig.log.read_since(0)[0])
    finally:
        conn.stop(timeout=1, announce=False)
        rig.bus._session._http.close()
        rig.log.close()
