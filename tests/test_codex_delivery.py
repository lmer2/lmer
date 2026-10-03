"""Structured Codex delivery preserves text and never retries an ambiguous send."""
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from lmer_cli import codex_delivery, comms, supervisor
from tests.conftest import strip_lmer_env


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    instance = codex_delivery.CodexDelivery(['codex'], notify=Mock())
    instance.directory = tmp_path
    instance.outbox = tmp_path / 'delivery.json'
    instance._connect = Mock()
    instance._disconnect = Mock()
    thread = {
        'id': 'thread-1', 'parentThreadId': None, 'threadSource': 'user',
        'model': 'gpt-6-astra', 'reasoningEffort': 'high',
        'turns': [{'id': 'turn-1', 'status': 'inProgress', 'items': []}],
    }

    def request(method, params):
        if method == 'thread/loaded/list':
            return {'data': ['thread-1']}
        if method == 'thread/read':
            return {'thread': thread}
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1'}}
        raise AssertionError(method)

    instance._request = Mock(side_effect=request)
    return instance, thread


def confirm(thread, text, ident='message-1'):
    thread['turns'][0]['items'].append({
        'type': 'userMessage', 'clientId': ident,
        'content': [{'type': 'text', 'text': text, 'text_elements': []}],
    })


def test_acceptance_is_not_confirmation_and_repeated_id_is_not_replayed(adapter):
    delivery, thread = adapter
    text = '! exact text\n/slash — café\n'
    receipt = delivery.deliver(text, client_id='message-1')
    assert receipt['state'] == 'accepted'
    assert receipt['submit_confirmed'] is False
    assert 'text' not in receipt
    assert json.loads(delivery.outbox.read_text())['message-1']['text'] == text
    assert delivery.outbox.stat().st_mode & 0o777 == 0o600
    request = [call for call in delivery._request.call_args_list if call.args[0] == 'turn/start']
    assert request[0].args[1] == {
        'threadId': 'thread-1', 'clientUserMessageId': 'message-1',
        'input': [{'type': 'text', 'text': text, 'text_elements': []}],
    }
    assert delivery.deliver(text, client_id='message-1') == receipt
    confirm(thread, text)
    delivery.refresh()
    result = delivery.deliver(text, client_id='message-1')
    assert result['state'] == 'confirmed' and result['submit_confirmed']
    assert len([call for call in delivery._request.call_args_list if call.args[0] == 'turn/start']) == 1
    assert 'text' not in json.loads(delivery.outbox.read_text())['message-1']


def test_reusing_id_for_different_payload_is_loud(adapter):
    delivery, _ = adapter
    delivery.deliver('original', client_id='message-1')
    with pytest.raises(codex_delivery.CodexDeliveryError, match='different text'):
        delivery.deliver('changed', client_id='message-1')


def test_cancelled_turn_retains_payload_without_replay(adapter):
    delivery, thread = adapter
    delivery.deliver('answer', client_id='message-1')
    thread['turns'][0]['status'] = 'interrupted'
    delivery.refresh()
    receipt = delivery.deliver('answer', client_id='message-1')
    assert receipt['state'] == 'uncertain' and not receipt['submit_confirmed']
    assert json.loads(delivery.outbox.read_text())['message-1']['text'] == 'answer'
    assert len([call for call in delivery._request.call_args_list if call.args[0] == 'turn/start']) == 1
    delivery.notify.assert_called_once()
    confirm(thread, 'answer')
    delivery.refresh()
    assert delivery.snapshot()['receipts'][0]['state'] == 'confirmed'


def test_lost_response_does_not_authorize_retry(adapter):
    delivery, _ = adapter
    original = delivery._request.side_effect

    def request(method, params):
        if method == 'turn/start':
            raise TimeoutError('connection lost after send')
        return original(method, params)

    delivery._request.side_effect = request
    receipt = delivery.deliver('preserved', client_id='message-1')
    assert receipt['state'] == 'uncertain'
    assert delivery.deliver('preserved', client_id='message-1') == receipt
    assert len([call for call in delivery._request.call_args_list if call.args[0] == 'turn/start']) == 1
    assert json.loads(delivery.outbox.read_text())['message-1']['text'] == 'preserved'


def test_confirmation_requires_original_bytes_not_normalized_whitespace(adapter):
    delivery, thread = adapter
    delivery.deliver('keep\nspacing', client_id='message-1')
    confirm(thread, 'keep spacing')
    delivery.refresh()
    assert delivery.snapshot()['receipts'][0]['state'] == 'uncertain'
    assert json.loads(delivery.outbox.read_text())['message-1']['text'] == 'keep\nspacing'


def test_ambiguous_primary_threads_are_not_guessed(adapter):
    delivery, _ = adapter
    original = delivery._request.side_effect
    delivery._request.side_effect = lambda method, params: (
        {'data': ['thread-1', 'thread-2']} if method == 'thread/loaded/list' else original(method, params)
    )
    receipt = delivery.deliver('keep pending')
    assert receipt['state'] == 'rejected'
    assert not any(call.args[0] == 'turn/start' for call in delivery._request.call_args_list)


def test_runtime_mismatch_comes_from_native_thread(adapter, monkeypatch):
    delivery, thread = adapter
    monkeypatch.setenv('LMER_LLM_NAME', 'gpt-6-astra')
    monkeypatch.setenv('LMER_REASONING_EFFORT', 'high')
    thread.update(model='gpt-5.6-luna', reasoningEffort='low')
    delivery.deliver('retain current selection')
    assert delivery.snapshot()['runtime']['preset_mismatch']
    assert 'gpt-5.6-luna/low' in delivery.notify.call_args.args[0]


def test_api_routes_automated_codex_input_without_any_terminal_write(adapter, monkeypatch):
    delivery, _ = adapter
    monkeypatch.setenv('LMER_HARNESS', 'codex')
    write = Mock(side_effect=AssertionError('automated delivery touched the terminal'))
    submit = Mock(side_effect=AssertionError('automated delivery pressed Enter'))
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), write, 'secret', submit=submit, codex_delivery=delivery,
    )
    client = TestClient(app)
    text = '! preserve\n/slash — exact'
    response = client.post('/input', headers={'Authorization': 'Bearer secret'}, json={
        'data': text, 'append_newline': True, 'sanitize': True, 'delivery_id': 'api-1',
    })
    assert response.status_code == 200
    assert response.json()['transport'] == 'codex-app-server'
    assert json.loads(delivery.outbox.read_text())['api-1']['text'] == text
    write.assert_not_called()
    submit.assert_not_called()


def test_bus_delivery_uses_record_identity_and_preserves_payload(adapter):
    delivery, _ = adapter
    log = comms.MessageLog('s-1')
    message, _ = log.append(
        kind=comms.KIND_MESSAGE, text='preserve this', source=comms.Source(comms.CHANNEL_PLATFORM),
    )
    submit = Mock(side_effect=AssertionError('bus delivery touched PTY'))
    receipt = supervisor._relay_message(log, message, submit, 'codex', delivery)
    assert receipt['delivery_id'] == message.id == 's-1/000001'
    submit.assert_not_called()


@pytest.mark.parametrize('answer', [False, True])
@pytest.mark.parametrize('lost_response', [False, True])
def test_real_bus_routes_preserve_generated_identity_and_never_replay(adapter, tmp_path, answer, lost_response):
    delivery, thread = adapter
    original = delivery._request.side_effect

    def request(method, params):
        if method == 'turn/start' and lost_response:
            raise TimeoutError('native response lost after submission')
        return original(method, params)

    delivery._request.side_effect = request
    log = comms.MessageLog('route-session', path=str(tmp_path / 'bus.jsonl'))
    terminal = Mock(side_effect=AssertionError('bus touched PTY'))
    client = TestClient(supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), terminal, 'secret', comms=log, codex_delivery=delivery,
    ), raise_server_exceptions=False)
    auth = {'Authorization': 'Bearer secret'}
    body = {'kind': 'message', 'text': '! original bus text\n/slash — café',
            'source': {'channel': 'platform', 'ref': 'original-ref'}}
    expected = '[platform] ' + body['text']
    try:
        if answer:
            question = client.post('/messages', headers=auth, json={
                'kind': 'question', 'text': 'unclaimed question', 'source': {'channel': 'agent'},
            })
            assert question.status_code == 200 and question.json()['relay'] is None
            body.update(kind='answer', in_reply_to=question.json()['message']['id'])
            expected = "[platform] (answer to #1 'unclaimed question') " + body['text']
        response = client.post('/messages', headers=auth, json=body)
        assert response.status_code == 200, response.text
        record = log.get(2 if answer else 1)
        assert record.id == f'route-session/{record.seq:06d}'
        receipt = response.json()['relay']
        assert receipt['delivery_id'] == record.id
        assert receipt['state'] == ('uncertain' if lost_response else 'accepted')
        assert record.text == body['text']
        assert json.loads(delivery.outbox.read_text())[record.id]['text'] == expected
        starts = [c for c in delivery._request.call_args_list if c.args[0] == 'turn/start']
        assert len(starts) == 1
        assert starts[0].args[1] == {'threadId': thread['id'], 'clientUserMessageId': record.id,
                                    'input': [{'type': 'text', 'text': expected, 'text_elements': []}]}
        repeated = client.post('/messages', headers=auth, json=body)
        assert repeated.status_code == 200 and repeated.json()['relay'] is None
        assert delivery.deliver(expected, client_id=record.id)['state'] == receipt['state']
        with pytest.raises(codex_delivery.CodexDeliveryError, match='different text'):
            delivery.deliver(expected + ' changed', client_id=record.id)
        assert len([c for c in delivery._request.call_args_list if c.args[0] == 'turn/start']) == 1
        terminal.assert_not_called()
    finally:
        client.close()
        log.close()


@pytest.mark.parametrize('harness', ['claude', 'pi'])
def test_other_runners_keep_existing_supervisor_path(monkeypatch, harness):
    monkeypatch.setenv('LMER_HARNESS', harness)
    run = Mock(return_value=17)
    monkeypatch.setattr(supervisor, '_run_supervisor', run)
    assert supervisor.run_supervisor([harness], {}) == 17
    assert 'codex_delivery' not in run.call_args.kwargs


def test_fresh_idle_thread_does_not_require_unsupported_history(adapter):
    delivery, _ = adapter
    original = delivery._request.side_effect

    def request(method, params):
        assert not params.get('includeTurns'), 'fresh idle history is unsupported'
        return original(method, params)

    delivery._request.side_effect = request
    assert delivery.deliver('first input')['state'] == 'accepted'


def test_ephemeral_title_thread_is_not_read_as_persistent_history(adapter):
    delivery, _ = adapter
    original = delivery._request.side_effect

    def request(method, params):
        if method == 'thread/loaded/list':
            return {'data': ['title', 'thread-1']}
        if params.get('threadId') == 'title':
            assert params['includeTurns'] is False
            return {'thread': {'id': 'title', 'ephemeral': True}}
        return original(method, params)

    delivery._request.side_effect = request
    assert delivery.deliver('first input')['state'] == 'accepted'
    delivery.refresh()


def test_thread_switch_is_not_silently_retargeted(adapter):
    delivery, thread = adapter
    delivery.deliver('original')
    thread['id'] = 'different'
    assert delivery.deliver('new input')['state'] == 'rejected'
    assert len([call for call in delivery._request.call_args_list if call.args[0] == 'turn/start']) == 1


@pytest.mark.parametrize('args', [['--remote', 'unix:///foreign'], ['--profile=other'], ['--ephemeral']])
def test_unsupported_backend_ownership_is_refused_before_start(args):
    delivery = codex_delivery.CodexDelivery(['codex', *args])
    with pytest.raises(codex_delivery.CodexDeliveryError):
        delivery.start()
    assert delivery.backend is None and delivery.directory is None


def test_private_backend_launch_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_delivery.tempfile, 'mkdtemp', lambda **_: str(tmp_path))
    process = Mock(pid=9999999)
    process.poll.return_value = None

    def launch(command, **kwargs):
        (tmp_path / 'server.sock').touch()
        assert command == ['codex', '-c', 'check_for_update_on_startup=false',
                           '-c', 'model="gpt-6-astra"', '-c', 'model_reasoning_effort=high',
                           'app-server', '--listen', 'unix://' + str(tmp_path / 'server.sock')]
        assert kwargs['start_new_session'] and 'env' not in kwargs
        assert kwargs['stdin'] == codex_delivery.subprocess.DEVNULL
        return process

    monkeypatch.setattr(codex_delivery.subprocess, 'Popen', launch)
    kill = Mock()
    monkeypatch.setattr(codex_delivery.os, 'killpg', kill)
    with codex_delivery.CodexDelivery(['codex', '--model', 'gpt-6-astra', '-c', 'model_reasoning_effort=high']) as delivery:
        command = delivery.start()
        assert command[1:3] == ['--remote', 'unix://' + str(tmp_path / 'server.sock')]
        assert delivery.socket.stat().st_mode & 0o777 == 0o600
    kill.assert_called_once_with(process.pid, codex_delivery.signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=5)
    assert not tmp_path.exists()


def test_raw_terminal_input_is_still_exact_with_codex_adapter(adapter):
    delivery, _ = adapter
    write = Mock(return_value=1)
    client = TestClient(supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), write, 'secret', codex_delivery=delivery,
    ))
    response = client.post('/input', headers={'Authorization': 'Bearer secret'}, json={'data': '\x1b', 'append_newline': False})
    assert response.status_code == 200
    write.assert_called_once_with(b'\x1b')
    delivery._request.assert_not_called()


def test_outbox_failure_prevents_native_submission(adapter):
    delivery, _ = adapter
    delivery._persist = Mock(side_effect=OSError('disk full'))
    with pytest.raises(codex_delivery.CodexDeliveryError, match='native submission refused'):
        delivery.deliver('preserve this')
    delivery._request.assert_not_called()
    assert delivery.snapshot()['receipts'] == []
    assert 'was not sent' in delivery.notify.call_args.args[0]


@pytest.mark.parametrize('settled', [False, True])
def test_runtime_monitor_observes_manual_changes_without_submitting(adapter, monkeypatch, settled):
    delivery, thread = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    monkeypatch.setenv('LMER_LLM_NAME', 'gpt-6-astra')
    monkeypatch.setenv('LMER_REASONING_EFFORT', 'high')
    if settled:
        delivery.deliver('original', client_id='message-1')
        confirm(thread, 'original')
    delivery.refresh()
    assert delivery.snapshot()['runtime']['model'] == 'gpt-6-astra'
    delivery._request.reset_mock()
    thread.update(model='gpt-5.6-luna', reasoningEffort='low')
    for _ in range(10):
        delivery.refresh()
    delivery._request.assert_not_called()
    clock[0] += 5
    delivery.refresh()
    runtime = delivery.snapshot()['runtime']
    assert (runtime['model'], runtime['effort'], runtime['preset_mismatch']) == ('gpt-5.6-luna', 'low', True)
    assert not any(call.args[0] == 'turn/start' for call in delivery._request.call_args_list)
    assert not any(call.args[1].get('includeTurns') for call in delivery._request.call_args_list)
    assert 'gpt-5.6-luna/low' in delivery.notify.call_args.args[0]


def test_pre_submission_failure_can_retry_same_original_after_reconnect(adapter):
    delivery, _ = adapter
    delivery._connect.side_effect = OSError('connection unavailable before submission')
    first = delivery.deliver('original', client_id='message-1')
    assert first['state'] == 'rejected' and first['retryable']
    delivery._request.assert_not_called()
    delivery._connect.side_effect = None
    receipt = delivery.deliver('original', client_id='message-1')
    assert receipt['state'] == 'accepted' and not receipt['retryable']
    delivery.deliver('original', client_id='message-1')
    assert len([c for c in delivery._request.call_args_list if c.args[0] == 'turn/start']) == 1


def test_native_rejection_does_not_enable_automatic_retry(adapter):
    delivery, _ = adapter
    original = delivery._request.side_effect

    def request(method, params):
        if method == 'turn/start':
            raise codex_delivery.CodexDeliveryError('native rejection')
        return original(method, params)

    delivery._request.side_effect = request
    receipt = delivery.deliver('original', client_id='message-1')
    assert receipt['state'] == 'rejected' and not receipt['retryable']
    assert delivery.deliver('original', client_id='message-1') == receipt
    assert len([c for c in delivery._request.call_args_list if c.args[0] == 'turn/start']) == 1
