"""Operator recovery goes through the executable CLI and durable lease."""
import json

import pytest

from lmer_cli import comms
from lmer_cli.connector_delivery import checkpoint_path, marker_path, receipt_path
from lmer_cli.connector_ownership import read_json
from tests.timing_budgets import SETTLE, STOP, XDIST_GROUP
from tests.test_connector_process import child, wait_for
from tests.connector_fakes import isolated_connectors, live_supervisor

pytestmark = pytest.mark.xdist_group(XDIST_GROUP)


def block(child, mode="unknown"):
    (child.root / "mode").write_text(mode)
    source, _ = child.supervisor.log.append(kind="question", text="choose next step", source=comms.Source("agent"), options=["continue", "stop"])
    owned = child.spawn()
    wait_for(lambda: owned.popen.poll() is not None, child)
    assert owned.collect() == 4
    result = child.cli("inspect")
    assert result.returncode == 0, result.stderr
    return source, json.loads(result.stdout)


def test_explicit_retry_cli_selects_one_operation_and_retains_source(child):
    source, inspection = block(child)
    original = checkpoint_path(child.key, child.supervisor.log.session).read_bytes()
    op = inspection["operation"]
    for args in (("retry", "--operation", "wrong", "--inspected-thread", "--accept-possible-duplicate"),
                 ("retry", "--operation", op)):
        denied = child.cli(*args)
        assert denied.returncode == 2, denied.stdout
        assert checkpoint_path(child.key, child.supervisor.log.session).read_bytes() == original
    result = child.cli("retry", "--operation", op, "--inspected-thread", "--accept-possible-duplicate")
    assert result.returncode == 0, result.stderr
    assert "duplicate" in result.stderr
    saved = read_json(checkpoint_path(child.key, child.supervisor.log.session))
    assert saved["pending"]["source"]["id"] == source.id
    assert saved["pending"]["source"]["options"] == ["continue", "stop"]
    assert child.cli("retry", "--operation", op, "--inspected-thread", "--accept-possible-duplicate").returncode == 2
    (child.root / "mode").write_text("healthy")
    owned = child.spawn()
    try:
        wait_for(lambda: len(child.events()) == 2, child)
        wait_for(lambda: read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child)
        assert all("choose next step" in event["text"] for event in child.events())
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        owned.stop(timeout=STOP)


@pytest.mark.parametrize("mode", ["receipt-failure", "receipt-saved"])
def test_verified_receipt_cli_finishes_bookkeeping_without_resend(child, mode):
    source, inspection = block(child, mode)
    extra = ["--remote-ref", child.events()[0]["ref"]] if mode == "receipt-failure" else []
    if mode == "receipt-saved":
        denied = child.cli("retry", "--operation", inspection["operation"],
                           "--inspected-thread", "--accept-possible-duplicate")
        assert denied.returncode == 2 and "confirmed sends cannot be retried" in denied.stderr
    result = child.cli("receipt", "--operation", inspection["operation"], *extra)
    assert result.returncode == 0, result.stderr
    (child.root / "mode").write_text("healthy")
    owned = child.spawn()
    try:
        wait_for(lambda: read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child)
        assert len(child.events()) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        owned.stop(timeout=STOP)


@pytest.mark.parametrize("interrupted", [False, True])
def test_ledger_confirmation_blocks_retry_after_checkpoint_failure(child, interrupted):
    source, inspection = block(child, "receipt-failure")
    operation = inspection["operation"]
    checkpoint = checkpoint_path(child.key, child.supervisor.log.session)
    before = checkpoint.read_bytes()
    marker = marker_path(child.key).read_bytes()
    saved = read_json(checkpoint)
    assert not saved["pending"]["parts"]["text"].get("receipt")
    reference = child.events()[0]["ref"]
    assert read_json(receipt_path(child.key, reference))["operation"] == operation

    denied = child.cli("retry", "--operation", operation,
                       "--inspected-thread", "--accept-possible-duplicate")
    assert denied.returncode == 2, denied.stdout
    assert "confirmed sends cannot be retried" in denied.stderr
    assert checkpoint.read_bytes() == before
    assert marker_path(child.key).read_bytes() == marker
    inspected = json.loads(child.cli("inspect").stdout)
    assert inspected["receipt"] == reference
    assert inspected["status"] == "confirmed"
    assert inspected["possible_duplicate"] is False

    args = ("receipt", "--operation", operation)
    if interrupted:
        (child.root / "mode").write_text("interrupt-recovery")
        result = child.cli(*args)
        assert result.returncode == 71, result.stderr
        assert marker_path(child.key).read_bytes() == marker
        assert child.cli("retry", "--operation", operation,
                         "--inspected-thread", "--accept-possible-duplicate").returncode == 2
    result = child.cli(*args)
    assert result.returncode == 0, result.stderr
    recovered = read_json(checkpoint)
    assert recovered["pending"]["source"] == saved["pending"]["source"]
    assert recovered["transport"] == saved["transport"]
    assert recovered["pending"]["parts"]["text"]["receipt"] == reference
    assert child.cli(*args).returncode == 2
    (child.root / "mode").write_text("healthy")
    owned = child.spawn()
    try:
        wait_for(lambda: read_json(checkpoint)["pending"] is None, child)
        assert len(child.events()) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        owned.stop(timeout=STOP)


@pytest.mark.parametrize("conflict", ["identity", "second-receipt"])
def test_recovery_cli_preserves_conflicting_ledger_evidence(child, conflict):
    _, inspection = block(child, "receipt-failure")
    operation = inspection["operation"]
    path = receipt_path(child.key, child.events()[0]["ref"])
    owner = read_json(path)
    if conflict == "identity":
        owner["identity"] = "different-payload"
    else:
        owner["receipt"] = "another-confirmation"
        path = receipt_path(child.key, owner["receipt"])
    path.write_text(json.dumps(owner))
    preserved = {p: p.read_bytes() for p in (
        checkpoint_path(child.key, child.supervisor.log.session), marker_path(child.key), path,
    )}
    for args in (("inspect",), ("receipt", "--operation", operation),
                 ("retry", "--operation", operation, "--inspected-thread", "--accept-possible-duplicate")):
        result = child.cli(*args)
        assert result.returncode == 2, result.stdout
        assert "state preserved" in result.stderr
        assert all(p.read_bytes() == value for p, value in preserved.items())
        assert len(child.events()) == 1


def test_interrupted_retry_is_resumable_but_authorization_is_single_use(child):
    source, inspection = block(child, "unknown-every-send")
    op = inspection["operation"]
    (child.root / "mode").write_text("interrupt-recovery")
    args = ("retry", "--operation", op, "--inspected-thread", "--accept-possible-duplicate")
    interrupted = child.cli(*args)
    assert interrupted.returncode == 71, interrupted.stderr
    assert marker_path(child.key).exists()
    pending = read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"]
    assert pending["source"]["id"] == source.id
    assert child.cli(*args).returncode == 0
    (child.root / "mode").write_text("unknown-every-send")
    owned = child.spawn()
    wait_for(lambda: owned.popen.poll() is not None, child)
    assert owned.collect() == 4
    assert len(child.events()) == 2
    inspection2 = json.loads(child.cli("inspect").stdout)
    assert inspection2["operation"] != op
    assert child.cli(*args).returncode == 2
    missing_warning_acceptance = child.cli("retry", "--operation", inspection2["operation"])
    assert missing_warning_acceptance.returncode == 2
    assert "may create a duplicate" in missing_warning_acceptance.stderr


def test_recovery_cli_cannot_take_lease_from_live_child(child):
    (child.root / "mode").write_text("wait-after-send")
    child.supervisor.log.append(kind="milestone", text="still in flight", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: bool(child.events()), child)
        before = read_json(child.key.owner_path)["token"]
        result = child.cli("inspect")
        assert result.returncode == 2, result.stderr
        assert read_json(child.key.owner_path)["token"] == before
        assert owned.popen.poll() is None
    finally:
        (child.root / "mode").write_text("healthy")
        owned.stop(timeout=STOP)


def test_matrix_retry_preserves_confirmed_parts_same_name_indices_and_question(tmp_path, live_supervisor):
    from tests.connector_fakes import ChildHarness

    harness = ChildHarness(tmp_path / "matrix-files", "matrix", live_supervisor)
    uploads = harness.root / "uploads"
    uploads.mkdir()
    attachments = []
    for index in range(3):
        (uploads / str(index)).write_text("body-" + str(index))
        attachments.append(dict(name="same.txt", media_type="text/plain", bytes=6, path="/uploads/" + str(index)))
    source, _ = live_supervisor.log.append(kind="question", text="check all files", source=comms.Source("agent"),
                                          options=["yes", "no"], attachments=attachments)
    (harness.root / "mode").write_text("second-file-unknown")
    try:
        owned = harness.spawn()
        wait_for(lambda: owned.popen.poll() is not None, harness)
        assert owned.collect() == 4
        inspection = json.loads(harness.cli("inspect").stdout)
        assert inspection["part"] == "file:1"
        saved = read_json(checkpoint_path(harness.key, live_supervisor.log.session))
        receipts = {part: value["receipt"] for part, value in saved["pending"]["parts"].items() if value.get("receipt")}
        assert set(receipts) == {"text", "file:0"}
        assert saved["transport"]["_question_events"][source.id] == receipts["text"]
        result = harness.cli("retry", "--operation", inspection["operation"], "--inspected-thread", "--accept-possible-duplicate")
        assert result.returncode == 0, result.stderr
        after = read_json(checkpoint_path(harness.key, live_supervisor.log.session))
        assert after["transport"] == saved["transport"]
        assert after["pending"]["source"] == saved["pending"]["source"]
        assert {part: after["pending"]["parts"][part]["receipt"] for part in receipts} == receipts
        (harness.root / "mode").write_text("healthy")
        resumed = harness.spawn()
        try:
            wait_for(lambda: read_json(checkpoint_path(harness.key, live_supervisor.log.session))["pending"] is None, harness, SETTLE)
            events = harness.events()
            assert len(events) == 5  # one question, three files, one explicitly accepted duplicate
            assert sum("check all files" in event["text"] for event in events) == 1
            assert len({event["file"]["url"] for event in events if event["kind"] == "m.file"}) == 3
            assert live_supervisor.log.read_since(0)[0][0].id == source.id
        finally:
            resumed.stop(timeout=STOP)
    finally:
        harness.close()


def test_receipt_cli_rejects_receipt_from_earlier_completed_identical_send(child):
    child.supervisor.log.append(kind="milestone", text="identical text", source=comms.Source("agent"))
    first = child.spawn()
    try:
        wait_for(lambda: bool(child.events()), child)
        wait_for(lambda: read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child)
    finally:
        first.stop(timeout=STOP)
    old_ref = child.events()[0]["ref"]
    (child.root / "mode").write_text("unknown")
    child.supervisor.log.append(kind="milestone", text="identical text", source=comms.Source("agent"))
    second = child.spawn()
    wait_for(lambda: second.popen.poll() is not None, child)
    assert second.collect() == 4
    inspection = json.loads(child.cli("inspect").stdout)
    before = checkpoint_path(child.key, child.supervisor.log.session).read_bytes()
    result = child.cli("receipt", "--operation", inspection["operation"], "--remote-ref", old_ref)
    assert result.returncode == 2, "the CLI accepted an earlier operation's identical receipt"
    assert checkpoint_path(child.key, child.supervisor.log.session).read_bytes() == before
    assert marker_path(child.key).exists()


@pytest.mark.parametrize("mismatch", ["session", "source"])
def test_recovery_cli_preserves_state_when_supervisor_or_source_disagrees(child, mismatch, monkeypatch):
    source, inspection = block(child)
    checkpoint = checkpoint_path(child.key, child.supervisor.log.session)
    before = checkpoint.read_bytes()
    marker = marker_path(child.key).read_bytes()
    journal = (child.root.parent / "source.jsonl").read_bytes()
    if mismatch == "session":
        monkeypatch.setattr(child.supervisor.log, "session", "different-supervisor")
    else:
        original_read = child.supervisor.log.read_since
        def unavailable(*args, **kwargs):
            records, cursor = original_read(*args, **kwargs)
            return [record for record in records if record.id != source.id], cursor
        monkeypatch.setattr(child.supervisor.log, "read_since", unavailable)
    for action, extra in (("retry", ["--inspected-thread", "--accept-possible-duplicate"]),
                          ("receipt", ["--remote-ref", child.events()[0]["ref"]])):
        result = child.cli(action, "--operation", inspection["operation"], *extra)
        assert result.returncode == 2, result.stdout
        assert ("supervisor identity" if mismatch == "session" else "source record") in result.stderr
        assert checkpoint.read_bytes() == before
        assert marker_path(child.key).read_bytes() == marker
        assert (child.root.parent / "source.jsonl").read_bytes() == journal


def test_interrupted_receipt_cli_resumes_saved_confirmation_without_send(child):
    source, inspection = block(child, "receipt-saved")
    (child.root / "mode").write_text("interrupt-recovery")
    args = ("receipt", "--operation", inspection["operation"])
    result = child.cli(*args)
    assert result.returncode == 71, result.stderr
    assert marker_path(child.key).exists()
    assert child.cli(*args).returncode == 0
    (child.root / "mode").write_text("healthy")
    resumed = child.spawn()
    try:
        wait_for(lambda: read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child)
        assert len(child.events()) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        resumed.stop(timeout=STOP)
