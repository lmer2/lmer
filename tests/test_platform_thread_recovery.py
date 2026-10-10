"""Host recovery uses the real lease, checkpoint and supervisor source journal."""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lmer_cli import comms
from lmer_cli.connector_delivery import DeliveryState, RecoveryRequired, checkpoint_path, marker_path
from lmer_cli.connector_ownership import Lease, OwnershipBusy, ThreadKey, read_json, write_json
from lmer_platform import api, assistant, config, connectors, ctl, spawn
from slack_chat.client import SlackError
from matrix_chat.client import MatrixError
from tests.connector_fakes import live_supervisor
from tests.conftest import strip_lmer_env
from tests.test_platform_ctl_threads import _Transport
from tests.test_platform_thread_connectors import (
    MATRIX_LINK, OTHER_SLACK_LINK, SLACK_LINK, FakeSlack, component, disconnect_notices,
    live_session, matrix_env, platform_root,
)
from tests.timing_budgets import JOIN, LINK, outlast, stretch


@pytest.fixture(autouse=True)
def clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(params=["slack", "matrix"])
def rig(request, platform_root, live_supervisor, monkeypatch):
    port = int(live_supervisor.endpoint.url.rsplit(":", 1)[1])
    live_session(port=port)
    spawn.token_file_for("s-1").write_text(live_supervisor.endpoint.token)
    now = [0.0]
    link = SLACK_LINK if request.param == "slack" else MATRIX_LINK
    environment = {"SLACK_BOT_TOKEN": "xoxb-test"} if request.param == "slack" else matrix_env()
    threads = component(platform_root, clock=lambda: now[0], environ=environment)
    record = threads.adopt_launch_targets("s-1", [link], by="operator")[0]
    ref = connectors.parse_thread(link)
    key = ThreadKey(ref.channel, *ref.registry_key())
    notices = []
    monkeypatch.setattr(assistant, "notify", lambda text, **kw: notices.append((text, kw)))
    connectors.register_connectors(threads)
    monkeypatch.setenv("LMER_PLATFORM_URL", "http://testserver")
    monkeypatch.setenv("LMER_PLATFORM_SECRET", "recovery-test-secret")
    client = TestClient(api.create_app(config.load(), "recovery-test-secret"))
    yield SimpleNamespace(threads=threads, supervisor=live_supervisor, key=key,
                          record=record, now=now, notices=notices, client=client)
    client.close()
    connectors.register_connectors(None)


def pending(rig, monkeypatch, outcome):
    rig.supervisor.log.append(kind="milestone", text="pending delivery", source=comms.Source("agent"))
    source = rig.supervisor.client.messages(0, timeout=0)[0][0]
    lease = Lease.acquire(rig.key)
    state = DeliveryState(rig.key, lease, snapshot=lambda: {"_bus_cursor": 0}, restore=lambda saved: None)
    state.bind(rig.supervisor.client.health())

    def send():
        if outcome == "unknown":
            raise TimeoutError("remote response lost")
        if outcome == "rejected":
            if rig.key.transport == "matrix":
                raise MatrixError("limited", status=429, errcode="M_LIMIT_EXCEEDED")
            raise SlackError("limited", code="ratelimited")
        return "1712345679.000100"

    def fail_clear(operation):
        raise OSError("injected marker removal failure")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(state, "clear_marker", fail_clear)
            with pytest.raises(RecoveryRequired):
                state.send(source, "text", {"text": source["text"]}, send)
    finally:
        state.close()
        lease.close()
    return read_json(marker_path(rig.key))["operation"]


def cli(rig, capsys, action, *args):
    code = ctl.main(["threads", "recover", "s-1", rig.record["id"], action, *args],
                    transport=_Transport(rig.client))
    output = capsys.readouterr()
    return code, output


def test_saved_receipt_recovers_and_starts_without_operator(rig, monkeypatch):
    pending(rig, monkeypatch, "confirmed")
    rig.threads.reconcile()
    view = rig.threads.listing("s-1")[0]
    assert view["running"]
    assert not view["attention"]
    assert not marker_path(rig.key).exists()
    assert len(rig.threads._runner.started) == 1
    assert not rig.notices


def test_unknown_outcome_stays_parked_with_health_log_and_no_digest(rig, monkeypatch, caplog):
    pending(rig, monkeypatch, "unknown")
    checkpoint = checkpoint_path(rig.key, rig.supervisor.log.session)
    before = (marker_path(rig.key).read_bytes(), checkpoint.read_bytes())
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            rig.now[0] += 60
            rig.threads.reconcile()
    assert not rig.threads._runner.started
    assert before == (marker_path(rig.key).read_bytes(), checkpoint.read_bytes())
    assert caplog.text.count("platform_thread_connector_unavailable") == 1
    assert not rig.notices
    command = f"lmer-ctl threads recover s-1 {rig.record['id']} inspect"
    assert command in caplog.text
    view = rig.threads.listing("s-1")[0]
    assert view["attention"] == "recovery-required"
    assert command in view["detail"]


def test_failure_logging_changes_only_with_reason_and_resets_after_start(rig, caplog):
    key = ("s-1", rig.record["id"])
    with caplog.at_level(logging.INFO):
        rig.threads._fail(key, "first failure")
        rig.threads._fail(key, "first failure")
        rig.threads._fail(key, "second failure")
        rig.now[0] = 60
        rig.threads.reconcile()
        rig.threads._fail(key, "second failure")
    assert caplog.text.count("platform_thread_connector_unavailable") == 3


@pytest.mark.parametrize("action,outcome", [("inspect", "unknown"), ("receipt", "confirmed"),
                                          ("retry", "rejected"), ("clear", "unknown")])
@pytest.mark.parametrize("caller", ["operator", "assistant"])
def test_four_actions_round_trip_to_host_state(rig, monkeypatch, capsys, caplog, action, outcome, caller):
    if caller == "assistant":
        monkeypatch.setattr(api, "active_assistant_credential", lambda: "assistant-recovery-test")
        monkeypatch.setenv("LMER_PLATFORM_SECRET", "assistant-recovery-test")
    operation = pending(rig, monkeypatch, outcome)
    checkpoint = checkpoint_path(rig.key, rig.supervisor.log.session)
    before = checkpoint.read_bytes()
    args = [] if action == "inspect" else ["--operation", operation]
    if action == "clear":
        args += ["--acknowledge-duplicate", "--reason", "restore the conversation"]
    with caplog.at_level(logging.INFO):
        code, output = cli(rig, capsys, action, *args)
    assert code == 0, output.err
    result = json.loads(output.out)
    assert result["inspection"]["operation"] == operation
    assert marker_path(rig.key).exists() == (action == "inspect")
    if action == "clear":
        assert f"by={caller}" in caplog.text
        assert "restore the conversation" in caplog.text
    if action in ("inspect", "clear"):
        assert checkpoint.read_bytes() == before


@pytest.mark.parametrize("action,args", [
    ("retry", []), ("clear", []),
    ("clear", ["--acknowledge-duplicate"]),
    ("clear", ["--reason", "resume"]),
])
def test_unsafe_or_unacknowledged_action_preserves_state(rig, monkeypatch, capsys, action, args):
    operation = pending(rig, monkeypatch, "unknown")
    before = marker_path(rig.key).read_bytes()
    code, output = cli(rig, capsys, action, "--operation", operation, *args)
    assert code != 0
    assert marker_path(rig.key).read_bytes() == before


def test_safe_retry_budget_survives_daemon_reconstruction(rig, monkeypatch, platform_root):
    pending(rig, monkeypatch, "rejected")
    rig.threads.reconcile()
    assert rig.threads.listing("s-1")[0]["running"]
    rig.threads.stop()
    path = checkpoint_path(rig.key, rig.supervisor.log.session)
    state = read_json(path)
    part = state["pending"]["parts"]["text"]
    assert part["automatic_retries"] == 1
    part["status"] = "rejected"
    write_json(path, state)
    write_json(marker_path(rig.key), {
        "version": 1, "thread": rig.key.parts, "session": rig.supervisor.log.session,
        "operation": part["id"], "part": "text", "source_digest": state["pending"]["source_digest"],
        "identity": part["identity"],
    })
    again = component(platform_root, clock=lambda: 1000, environ=dict(rig.threads._environ))
    again.reconcile()
    assert not again.listing("s-1")[0]["running"]
    assert marker_path(rig.key).exists()
    assert not again._runner.started


def test_interrupted_automatic_retry_consumes_only_one_authorization(rig, monkeypatch):
    pending(rig, monkeypatch, "rejected")
    original = type(marker_path(rig.key)).unlink

    def fail_unlink(path, *args, **kwargs):
        if path == marker_path(rig.key):
            raise OSError("interrupted before marker removal")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(type(marker_path(rig.key)), "unlink", fail_unlink)
        rig.threads.reconcile()
    assert marker_path(rig.key).exists()
    checkpoint = checkpoint_path(rig.key, rig.supervisor.log.session)
    part = read_json(checkpoint)["pending"]["parts"]["text"]
    assert part["status"] == "retry-authorized"
    assert part["automatic_retries"] == 1
    rig.now[0] = 60
    rig.threads.reconcile()
    assert rig.threads.listing("s-1")[0]["running"]
    assert not marker_path(rig.key).exists()
    assert read_json(checkpoint)["pending"]["parts"]["text"]["automatic_retries"] == 1


def test_active_owner_and_stale_operation_are_refused(rig, monkeypatch, capsys):
    operation = pending(rig, monkeypatch, "unknown")
    before = marker_path(rig.key).read_bytes()
    lease = Lease.acquire(rig.key)
    try:
        code, output = cli(rig, capsys, "inspect")
        assert code != 0 and "another connector owns" in output.err
    finally:
        lease.close()
    code, output = cli(rig, capsys, "clear", "--operation", operation + "stale",
                       "--acknowledge-duplicate", "--reason", "restore")
    assert code != 0 and "stale" in output.err
    assert marker_path(rig.key).read_bytes() == before


def test_api_requires_auth_and_real_boolean_acknowledgement(rig, monkeypatch):
    operation = pending(rig, monkeypatch, "unknown")
    url = f"/api/sessions/s-1/threads/{rig.record['id']}/recover"
    body = {"action": "clear", "operation": operation, "acknowledge_duplicate": "true", "reason": "restore"}
    assert rig.client.post(url, json=body).status_code == 401
    headers = {"Authorization": "Bearer recovery-test-secret"}
    response = rig.client.post(url, json=body, headers=headers)
    assert response.status_code == 400
    assert marker_path(rig.key).exists()
    assert rig.client.post(url.replace(rig.record["id"], "slack:missing"),
                           json={"action": "inspect"}, headers=headers).status_code == 404


def test_plain_retry_refuses_an_interrupted_uncertain_authorization(rig, monkeypatch, capsys):
    from lmer_cli.connector_recovery import Recovery

    operation = pending(rig, monkeypatch, "unknown")
    lease = Lease.acquire(rig.key)
    recovery = Recovery(lease)
    original = recovery._complete

    def leave_marker(marker, state):
        write_json(checkpoint_path(rig.key, marker["session"]), state)
        raise OSError("interrupted explicit authorization")

    try:
        monkeypatch.setattr(recovery, "_complete", leave_marker)
        with pytest.raises(OSError):
            recovery.retry(operation, rig.supervisor.client, inspected=True, accept_duplicate=True)
        monkeypatch.setattr(recovery, "_complete", original)
    finally:
        lease.close()
    rig.threads.reconcile()
    assert not rig.threads._runner.started
    code, output = cli(rig, capsys, "retry", "--operation", operation)
    assert code != 0 and "unknown" in output.err
    assert marker_path(rig.key).exists()


def test_receipt_reference_uses_host_transport_and_verifies_identity(rig, monkeypatch, capsys):
    operation = pending(rig, monkeypatch, "unknown")
    asked = []

    class Remote:
        def auth_test(self):
            return "UBOT"

        def get_replies(self, conversation, root):
            asked.append((conversation, root))
            return [{"ts": "remote-receipt", "thread_ts": root, "text": "pending delivery", "user": "UBOT"}]

        def get_event(self, conversation, reference):
            asked.append((conversation, reference))
            return {"event_id": "remote-receipt", "sender": "@lmer:example.org", "content": {
                "body": "pending delivery", "m.relates_to": {"rel_type": "m.thread", "event_id": rig.key.root},
            }}

    rig.threads._slack_client = lambda credential: Remote()
    rig.threads._matrix_client = lambda account: Remote()
    code, output = cli(rig, capsys, "receipt", "--operation", operation, "--remote-ref", "wrong-receipt")
    assert code != 0
    assert marker_path(rig.key).exists()
    code, output = cli(rig, capsys, "receipt", "--operation", operation, "--remote-ref", "remote-receipt")
    assert code == 0, output.err
    assert asked and all(call[0] == rig.key.conversation for call in asked)
    state = read_json(checkpoint_path(rig.key, rig.supervisor.log.session))
    assert state["pending"]["parts"]["text"]["receipt"] == "remote-receipt"
    assert not marker_path(rig.key).exists()


def test_transport_failure_preserves_pending_send_and_reports_reason(rig, monkeypatch):
    operation = pending(rig, monkeypatch, "unknown")
    before = marker_path(rig.key).read_bytes()

    def unavailable(*args):
        if rig.key.transport == "matrix":
            raise MatrixError("remote unavailable", status=503, errcode="M_UNKNOWN")
        raise SlackError("remote unavailable")

    rig.threads._slack_client = unavailable
    rig.threads._matrix_client = unavailable
    response = rig.client.post(f"/api/sessions/s-1/threads/{rig.record['id']}/recover", json={
        "action": "receipt", "operation": operation, "remote_ref": "selected-receipt",
    }, headers={"Authorization": "Bearer recovery-test-secret"})
    assert response.status_code == 502
    assert "remote unavailable" in response.json()["detail"]
    assert marker_path(rig.key).read_bytes() == before


@pytest.mark.parametrize("action", ["inspect", "receipt", "retry", "clear"])
def test_review_connector_credentials_cannot_recover(rig, monkeypatch, action):
    operation = pending(rig, monkeypatch, "unknown")
    before = marker_path(rig.key).read_bytes()
    credential = config.mint_connector_credential("example-bridge", rig.key.transport)
    response = rig.client.post(f"/api/sessions/s-1/threads/{rig.record['id']}/recover", json={
        "action": action, "operation": operation, "acknowledge_duplicate": True, "reason": "restore",
    }, headers={"Authorization": f"Bearer {credential}"})
    assert response.status_code == 403
    assert marker_path(rig.key).read_bytes() == before
    assert not rig.threads._runner.started


def test_health_recovery_never_calls_notifier_and_keeps_attention(rig, monkeypatch, caplog):
    pending(rig, monkeypatch, "unknown")
    port = int(rig.supervisor.endpoint.url.rsplit(":", 1)[1])
    live_session("s-2", port=port)
    spawn.token_file_for("s-2").write_text(rig.supervisor.endpoint.token)
    rig.threads.adopt_launch_targets("s-2", [OTHER_SLACK_LINK], by="operator")
    rig.threads._environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    calls = []

    def fail_notify(*args, **kwargs):
        calls.append(args)
        raise OSError("assistant store unavailable")

    monkeypatch.setattr(assistant, "notify", fail_notify)
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            rig.now[0] += 60
            rig.threads.reconcile()
    assert not calls
    assert rig.threads.listing("s-1")[0]["attention"] == "recovery-required"
    assert rig.threads.listing("s-2")[0]["running"]
    assert caplog.text.count("platform_thread_recovery_required") == 1
    assert marker_path(rig.key).exists()


def test_review_clear_audit_scrubs_reason_in_both_records(rig, monkeypatch, caplog):
    operation = pending(rig, monkeypatch, "unknown")
    secret = "synthetic-review-credential"
    with caplog.at_level(logging.INFO):
        response = rig.client.post(f"/api/sessions/s-1/threads/{rig.record['id']}/recover", json={
            "action": "clear", "operation": operation, "acknowledge_duplicate": True,
            "reason": f"restore after Authorization: Bearer {secret}",
        }, headers={"Authorization": "Bearer recovery-test-secret"})
    assert response.status_code == 200
    audits = [r.getMessage() for r in caplog.records
              if r.getMessage().startswith(("platform_thread_clear_requested", "platform_thread_recovered"))]
    assert len(audits) == 2
    for audit in audits:
        assert secret not in audit
        assert "restore after" in audit and "by=operator" in audit and operation in audit


@pytest.fixture
def held_receipt(rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(outlast(JOIN))
            if rig.key.transport == "slack":
                body = {"ok": True, "messages": [{"ts": "remote-receipt", "thread_ts": rig.key.root,
                                                "user": "UBOT", "text": "pending delivery"}]}
            else:
                body = {"event_id": "remote-receipt", "sender": "@lmer:example.org", "content": {
                    "body": "pending delivery", "m.relates_to": {"rel_type": "m.thread", "event_id": rig.key.root},
                }}
            self.respond(body)

        def do_POST(self):
            self.respond({"ok": True, "user_id": "UBOT"})

        def respond(self, body):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    from slack_chat.client import SlackClient
    monkeypatch.setattr(SlackClient, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    rig.threads._environ["LMER_MATRIX_HOMESERVER"] = SlackClient.BASE_URL
    rig.threads._slack_client = connectors._slack_client
    rig.threads._matrix_client = connectors._matrix_client
    try:
        yield entered, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(JOIN)
        assert not worker.is_alive()


@pytest.mark.parametrize("change", ["none", "disconnect", "replace", "session-exit", "supervisor-change"])
def test_review_stalled_receipt_leaves_other_threads_available(rig, monkeypatch, held_receipt, change):
    operation = pending(rig, monkeypatch, "unknown")
    # The receipt stays held until release; the product's 15s client timeout
    # would otherwise end it first and let a blocked reconcile through.
    monkeypatch.setattr(connectors, "REMOTE_TIMEOUT_SECONDS", outlast(JOIN))
    live_session("s-2", port=int(rig.supervisor.endpoint.url.rsplit(":", 1)[1]))
    spawn.token_file_for("s-2").write_text(rig.supervisor.endpoint.token)
    rig.threads._environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    with monkeypatch.context() as other_session:
        other_session.setattr(rig.threads, "_slack_client", lambda credential: FakeSlack())
        rig.threads.adopt_launch_targets("s-2", [OTHER_SLACK_LINK], by="operator")
    entered, release = held_receipt
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(rig.client.post, f"/api/sessions/s-1/threads/{rig.record['id']}/recover", json={
            "action": "receipt", "operation": operation, "remote_ref": "remote-receipt",
        }, headers={"Authorization": "Bearer recovery-test-secret"})
        try:
            assert entered.wait(LINK)
            other_threads = pool.submit(rig.threads.listing, "s-2").result(timeout=stretch(3))
            assert len(other_threads) == 1 and not other_threads[0]["running"]
            with pytest.raises(OwnershipBusy):
                Lease.acquire(rig.key)
            other = rig.client.post(f"/api/sessions/s-1/threads/{rig.record['id']}/recover",
                                    json={"action": "inspect"},
                                    headers={"Authorization": "Bearer recovery-test-secret"})
            assert other.status_code == 409 and "in progress" in other.json()["detail"]
            # JOIN, not seconds: reconcile's fsyncs stretch under disk load (#572).
            # What matters is that it finished while the receipt was still held.
            pool.submit(rig.threads.reconcile).result(timeout=JOIN)
            assert not request.done(), "reconcile waited for the held receipt"
            assert rig.threads.listing("s-2")[0]["running"]
            if change in ("disconnect", "replace"):
                rig.threads.disconnect("s-1", rig.record["id"])
                if change == "replace":
                    with monkeypatch.context() as replacement:
                        replacement.setattr(rig.threads, "_slack_client", lambda credential: FakeSlack())
                        rig.threads.adopt_launch_targets("s-1", [rig.record["permalink"]], by="operator")
            elif change == "session-exit":
                live_session(alive=False)
            elif change == "supervisor-change":
                spawn.token_file_for("s-1").write_text("replacement-supervisor-credential")
            release.set()
            response = request.result(timeout=JOIN)
            assert response.status_code == (200 if change == "none" else 409)
            assert len(rig.threads._runner.started) == (2 if change == "none" else 1)
        finally:
            release.set()


def test_review_remote_receipt_http_timeout_preserves_marker(rig, monkeypatch, held_receipt):
    operation = pending(rig, monkeypatch, "unknown")
    before = marker_path(rig.key).read_bytes()
    entered, release = held_receipt
    monkeypatch.setattr(connectors, "REMOTE_TIMEOUT_SECONDS", stretch(0.2), raising=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(rig.client.post, f"/api/sessions/s-1/threads/{rig.record['id']}/recover", json={
            "action": "receipt", "operation": operation, "remote_ref": "remote-receipt",
        }, headers={"Authorization": "Bearer recovery-test-secret"})
        try:
            assert entered.wait(LINK)
            response = request.result(timeout=stretch(3))
            assert response.status_code == 502
            assert marker_path(rig.key).read_bytes() == before
            assert not rig.threads._runner.started
            lease = Lease.acquire(rig.key)
            lease.close()
        finally:
            release.set()
