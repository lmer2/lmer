"""Structured Codex delivery preserves text and never retries an ambiguous send."""
import functools
import json
from pathlib import Path
import re
import threading
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
        if method == 'thread/turns/list':
            return {'data': list(reversed(thread['turns'])), 'nextCursor': None}
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
    # A turn end is a state to keep watching, not a verdict to announce.
    delivery.notify.assert_not_called()
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


def settled(delivery, ident):
    """The receipt once the relay worker has submitted *ident*."""
    for _ in range(500):
        found = [r for r in delivery.snapshot()['receipts'] if r['delivery_id'] == ident]
        if found and found[0]['state'] != 'pending':
            return found[0]
        threading.Event().wait(.01)
    raise AssertionError(f'{ident} was never submitted')


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
        assert response.json()['relay'] == {'queued': True, 'bytes_written': 0}
        receipt = settled(delivery, record.id)
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


def starts(delivery):
    return [call for call in delivery._request.call_args_list if call.args[0] == 'turn/start']


def test_delayed_observation_later_confirms_with_one_note_per_outage(adapter):
    delivery, thread = adapter
    delivery.escalate = Mock()
    original = delivery._request.side_effect
    down = [True]

    def request(method, params):
        if method == 'thread/turns/list' and down[0]:
            raise TimeoutError('timed out in 10.0s')
        return original(method, params)

    delivery._request.side_effect = request
    delivery.deliver('slow host', client_id='message-1')
    for outage in range(2):
        down[0] = True
        delivery.refresh()
        delivery.refresh()
        down[0] = False
        if outage == 1:
            confirm(thread, 'slow host')
        delivery.refresh()
    notes = [call.args[0] for call in delivery.notify.call_args_list]
    assert [n for n in notes if 'unavailable' in n and 'message-1' in n] and len(
        [n for n in notes if 'unavailable' in n]) == 2
    assert len([n for n in notes if 'recovered' in n]) == 2
    assert delivery.snapshot()['receipts'][0]['state'] == 'confirmed'
    assert len(starts(delivery)) == 1
    delivery.escalate.assert_not_called()


def test_input_never_submitted_is_retried_exactly_once(adapter, monkeypatch):
    delivery, thread = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    original = delivery._request.side_effect
    discovery_down = [True]

    def request(method, params):
        if method == 'thread/loaded/list' and discovery_down[0]:
            raise TimeoutError('discovery timed out')
        return original(method, params)

    delivery._request.side_effect = request
    first = delivery.deliver('retry me', client_id='message-1', adapter_converges=True)
    assert first['state'] == 'retrying' and first['retryable']
    delivery.refresh()
    assert starts(delivery) == [], 'no retry before the interval'
    discovery_down[0] = False
    clock[0] += codex_delivery.RETRY_INTERVAL
    delivery.refresh()
    assert len(starts(delivery)) == 1
    confirm(thread, 'retry me')
    for _ in range(3):
        clock[0] += codex_delivery.RETRY_INTERVAL
        delivery.refresh()
    assert delivery.snapshot()['receipts'][0]['state'] == 'confirmed'
    assert len(starts(delivery)) == 1


def test_undecidable_input_escalates_once_and_closes_on_late_confirmation(adapter, monkeypatch):
    delivery, thread = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    delivery.escalate = Mock(return_value='s-1/000009')
    delivery.deescalate = Mock()
    delivery.deliver('held by codex\nsecond line', client_id='s-1/000038', adapter_converges=True)
    thread['turns'][0]['status'] = 'completed'
    thread['status'] = {'type': 'idle'}
    delivery.refresh()
    assert delivery.snapshot()['receipts'][0]['state'] == 'uncertain'
    delivery.escalate.assert_not_called()
    clock[0] += codex_delivery.CONFIRM_DEADLINE
    delivery.refresh()
    delivery.refresh()
    delivery.escalate.assert_called_once()
    receipt, reason, text = delivery.escalate.call_args.args
    assert receipt['delivery_id'] == 's-1/000038' and 'text' not in receipt
    assert 'idle' in reason and text == 'held by codex\nsecond line'
    assert len(starts(delivery)) == 1, 'escalation never resends'
    thread['turns'].append({'id': 'turn-2', 'status': 'completed', 'items': []})
    confirm({'turns': [thread['turns'][1]]}, 'held by codex\nsecond line', 's-1/000038')
    clock[0] += codex_delivery.RUNTIME_INTERVAL
    delivery.refresh()
    assert delivery.snapshot()['receipts'][0]['state'] == 'confirmed'
    delivery.deescalate.assert_called_once()
    assert delivery.deescalate.call_args.args[0] == 's-1/000009'


def test_native_rejection_escalates_at_once(adapter):
    delivery, _ = adapter
    delivery.escalate = Mock(return_value=None)
    original = delivery._request.side_effect
    delivery._request.side_effect = lambda method, params: (
        (_ for _ in ()).throw(codex_delivery.CodexDeliveryError('native rejection'))
        if method == 'turn/start' else original(method, params)
    )
    delivery.deliver('refused', client_id='message-1', adapter_converges=True)
    delivery.refresh()
    delivery.refresh()
    delivery.escalate.assert_called_once()
    assert 'rejected' in delivery.escalate.call_args.args[1]


def test_codex_without_paged_turns_falls_back_to_the_full_read(adapter):
    delivery, thread = adapter
    original = delivery._request.side_effect

    def request(method, params):
        if method == 'thread/turns/list':
            raise codex_delivery.CodexDeliveryError('Codex rejected thread/turns/list: unknown variant',
                                                    rpc_code=-32600)
        return original(method, params)

    delivery._request.side_effect = request
    delivery.deliver('older codex', client_id='message-1')
    confirm(thread, 'older codex')
    delivery.refresh()
    delivery.refresh()
    assert delivery.snapshot()['receipts'][0]['state'] == 'confirmed'
    methods = [call.args[0] for call in delivery._request.call_args_list]
    assert methods.count('thread/turns/list') == 1, 'the rejection is remembered for the run'
    assert any(call.args[1].get('includeTurns') for call in delivery._request.call_args_list)


def test_paged_turn_read_stops_at_the_oldest_pending_input(adapter):
    delivery, thread = adapter
    pages = {
        None: {'data': [{'id': 'turn-9', 'status': 'inProgress', 'items': [], 'startedAt': 10 ** 10}],
               'nextCursor': 'older'},
        'older': {'data': [{'id': 'turn-1', 'status': 'completed', 'items': [], 'startedAt': 1}],
                  'nextCursor': 'oldest'},
    }
    original = delivery._request.side_effect
    delivery._request.side_effect = lambda method, params: (
        pages[params.get('cursor')] if method == 'thread/turns/list' else original(method, params)
    )
    delivery.deliver('recent', client_id='message-1')
    delivery.refresh()
    reads = [call.args[1] for call in delivery._request.call_args_list if call.args[0] == 'thread/turns/list']
    assert [read.get('cursor') for read in reads] == [None, 'older']
    assert reads[0]['sortDirection'] == 'desc' and reads[0]['itemsView'] == 'full'


@pytest.mark.parametrize('raw, expected', [
    (None, 10.0), ('', 10.0), ('2.5', 2.5), ('0', 10.0), ('-1', 10.0), ('nan', 10.0), ('slow', 10.0), ('601', 10.0),
])
def test_rpc_timeout_comes_from_the_environment(raw, expected):
    env = {} if raw is None else {codex_delivery.RPC_TIMEOUT_ENV: raw}
    assert codex_delivery.rpc_timeout(env) == expected


def test_runtime_reports_the_effective_timeout_and_latency(adapter, monkeypatch):
    monkeypatch.setenv(codex_delivery.RPC_TIMEOUT_ENV, '4')
    delivery = codex_delivery.CodexDelivery(['codex'], notify=Mock())
    assert delivery.rpc_timeout == 4.0
    delivery._latency.extend([(0.1, False)] * 98 + [(4.0, True), (3.0, False)])
    snapshot = delivery.snapshot()
    assert snapshot['runtime'] is None, 'reported before discovery ever succeeds'
    assert snapshot['rpc_timeout'] == 4.0
    assert snapshot['rpc_latency'] == {'count': 100, 'max': 4.0, 'p99': 3.0, 'timeouts': 1}


def test_escalation_is_an_off_thread_question_closed_by_a_late_confirmation(tmp_path):
    log = comms.MessageLog('s-1', path=str(tmp_path / 'bus.jsonl'))
    try:
        record, _ = log.append(kind=comms.KIND_MESSAGE, text='how is need inspection determined?\nmore',
                               source=comms.Source(comms.CHANNEL_SLACK, author='someone'))
        receipt = {'delivery_id': record.id}
        relayed = '[slack · someone] how is need inspection determined?\nmore'
        handle = supervisor._escalate_codex_delivery(log, receipt, 'Codex has been idle for 120s', relayed)
        assert supervisor._escalate_codex_delivery(log, receipt, 'again', relayed) == handle, 'a retry posts nothing new'
        question = log.get(2)
        assert handle == question.id and question.kind == comms.KIND_QUESTION
        assert question.source.channel == comms.CHANNEL_PLATFORM and comms.off_thread(question.to_dict())
        assert '(“how is need inspection determined?”)' in question.text
        assert 'more' not in question.text and 'not resent' in question.text and '[slack' not in question.text
        supervisor._deescalate_codex_delivery(log, handle, receipt)
        assert log.get(3).kind == comms.KIND_CLOSE and log.get(3).in_reply_to == handle
        assert comms.off_thread(log.get(3).to_dict()), 'a reply inherits the flag'
        supervisor._deescalate_codex_delivery(log, handle, receipt)  # already closed: quiet
        assert log.get(4) is None
    finally:
        log.close()


def test_only_the_supervisor_keeps_a_question_off_the_threads(tmp_path):
    log = comms.MessageLog('s-1', path=str(tmp_path / 'bus.jsonl'))
    try:
        posted, _ = log.append(kind=comms.KIND_QUESTION, text='hidden?', source=comms.Source(comms.CHANNEL_AGENT),
                               meta={comms.META_OFF_THREAD: True})
        assert not comms.off_thread(posted.to_dict())
    finally:
        log.close()


def test_a_peer_message_escalation_quotes_none_of_it(tmp_path):
    log = comms.MessageLog('s-1', path=str(tmp_path / 'bus.jsonl'))
    try:
        record, _ = log.append(kind=comms.KIND_MESSAGE, text='private peer request',
                               source=comms.Source('peer-ul', author='Other Project UL', ref='r-1'),
                               meta={'peer': {'session': 's-abc'}})
        supervisor._escalate_codex_delivery(
            log, {'delivery_id': record.id}, 'Codex rejected it', comms.relay_payload(record)[0])
        question = log.get(2)
        assert record.id in question.text
        assert 'peer' not in question.text and 'private peer request' not in question.text
    finally:
        log.close()


def test_a_slow_codex_relay_does_not_hold_the_post(adapter, tmp_path):
    """The poster gets its receipt while the adapter is still submitting."""
    delivery, _ = adapter
    release = threading.Event()
    original = delivery._request.side_effect
    delivery._request.side_effect = lambda method, params: (
        release.wait(10) and original(method, params) if method == 'turn/start' else original(method, params)
    )
    log = comms.MessageLog('route-session', path=str(tmp_path / 'bus.jsonl'))
    client = TestClient(supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), Mock(), 'secret', comms=log, codex_delivery=delivery,
    ), raise_server_exceptions=False)
    auth = {'Authorization': 'Bearer secret'}
    try:
        ids = []
        for text in ('first', 'second'):
            response = client.post('/messages', headers=auth, json={
                'kind': 'message', 'text': text, 'source': {'channel': 'platform'}})
            assert response.status_code == 200 and response.json()['relay'] == {'queued': True, 'bytes_written': 0}
            ids.append(response.json()['message']['id'])
        assert not release.is_set()
        release.set()
        assert [settled(delivery, ident)['state'] for ident in ids] == ['accepted', 'accepted']
        assert [call.args[1]['clientUserMessageId'] for call in starts(delivery)] == ids, 'in order'
    finally:
        release.set()
        client.close()
        log.close()


def test_cli_env_dict_forwards_the_codex_rpc_timeout():
    """The adapter runs in the container; a host-set value must reach it."""
    source = (Path(__file__).parent.parent / 'src' / 'lmer_cli' / 'cli.py').read_text()
    assert re.search(r"""["']LMER_CODEX_RPC_TIMEOUT["']\s*:\s*os\.environ\.get\(\s*["']LMER_CODEX_RPC_TIMEOUT["']\s*\)""", source)


def test_caller_owned_input_is_neither_retried_nor_asked_about(adapter, monkeypatch):
    """Its caller was told "rejected" and owns the retry."""
    delivery, _ = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    delivery.escalate = Mock()
    delivery._connect.side_effect = OSError('backend unreachable before submission')
    receipt = delivery.deliver('wind down', client_id='wind-1')
    assert receipt['state'] == 'rejected' and receipt['retryable']
    assert 'may be retried' in receipt['note']
    delivery._connect.side_effect = None
    for _ in range(3):
        clock[0] += codex_delivery.CONFIRM_DEADLINE
        delivery.refresh()
    assert starts(delivery) == []
    delivery.escalate.assert_not_called()
    notes = [call.args[0] for call in delivery.notify.call_args_list if 'wind-1' in call.args[0]]
    assert len(notes) == 1, 'one note for one rejection'


def test_a_transient_paging_error_does_not_abandon_paging_for_the_run(adapter):
    delivery, thread = adapter
    original = delivery._request.side_effect
    failures = [1]

    def request(method, params):
        if method == 'thread/turns/list' and failures[0]:
            failures[0] -= 1
            raise codex_delivery.CodexDeliveryError('Codex rejected thread/turns/list: busy', rpc_code=-32000)
        return original(method, params)

    delivery._request.side_effect = request
    delivery.deliver('paged', client_id='message-1')
    delivery.refresh()
    confirm(thread, 'paged')
    delivery.refresh()
    assert delivery.snapshot()['receipts'][0]['state'] == 'confirmed'
    assert not any(call.args[1].get('includeTurns') for call in delivery._request.call_args_list)


def test_escalated_inputs_are_watched_with_one_page(adapter, monkeypatch):
    delivery, thread = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    delivery.escalate = Mock(return_value='s-1/000002')
    original = delivery._request.side_effect
    delivery._request.side_effect = lambda method, params: (
        {'data': [{'id': 'turn-1', 'status': 'completed', 'items': [], 'startedAt': 10 ** 10}], 'nextCursor': 'more'}
        if method == 'thread/turns/list' else original(method, params)
    )
    delivery.deliver('lost', client_id='s-1/000001', adapter_converges=True)
    delivery.refresh()
    clock[0] += codex_delivery.CONFIRM_DEADLINE
    delivery.refresh()
    delivery.escalate.assert_called_once()
    delivery._request.reset_mock()
    clock[0] += codex_delivery.RUNTIME_INTERVAL
    delivery.refresh()
    pages = [call for call in delivery._request.call_args_list if call.args[0] == 'thread/turns/list']
    assert len(pages) == 1


def test_the_receipt_monitor_survives_a_failing_tick(adapter, monkeypatch):
    delivery, _ = adapter
    monkeypatch.setattr(codex_delivery, 'PENDING_INTERVAL', .01)
    ticks = []

    def refresh():
        ticks.append(1)
        raise OSError('outbox disk full')

    delivery.refresh = refresh
    delivery.start_monitor()
    try:
        for _ in range(500):
            if len(ticks) >= 3:
                break
            delivery._stop.wait(.01)
        assert len(ticks) >= 3 and delivery._monitor.is_alive()
        assert 'outbox disk full' in delivery.notify.call_args.args[0]
    finally:
        delivery._stop.set()
        delivery._monitor.join(timeout=5)


@pytest.mark.parametrize('outcome', ['uncertain', 'escalated'])
def test_a_startup_input_that_cannot_converge_releases_inbound(adapter, outcome):
    delivery, _ = adapter
    receipt = {'delivery_id': 'startup', 'state': 'uncertain' if outcome == 'uncertain' else 'retrying',
               'retryable': outcome == 'escalated', 'escalated': outcome == 'escalated'}
    delivery.deliver = Mock(return_value=receipt)
    started = threading.Event()
    worker = delivery.auto_start({'start_command': '/start', 'start_prompt': 'then this'},
                                 threading.Event(), started.set)
    worker.join(timeout=10)
    assert started.is_set()
    assert [call.args[0] for call in delivery.deliver.call_args_list] == ['/start'], 'the prompt is not sent'
    assert 'released' in delivery.notify.call_args.args[0]


def test_a_caller_owned_delivery_does_not_outwait_its_budget(adapter, monkeypatch):
    """Behind a long observation the caller hears "nothing sent", in bounded time."""
    delivery, _ = adapter
    delivery.rpc_timeout = .05
    held, done = threading.Event(), threading.Event()

    def observe():
        with delivery._lock:
            held.set()
            done.wait(5)

    holder = threading.Thread(target=observe)
    holder.start()
    held.wait(5)
    try:
        with pytest.raises(codex_delivery.CodexDeliveryError, match='busy observing'):
            delivery.deliver('api input', client_id='api-1')
        assert starts(delivery) == []
    finally:
        done.set()
        holder.join()
    assert delivery.snapshot()['receipts'] == []
    assert delivery.deliver('api input', client_id='api-1')['state'] == 'accepted'


def test_the_input_budget_covers_a_submission_and_the_lock_wait():
    assert codex_delivery.input_budget(10.0) == (codex_delivery.LOCK_WAIT_RPCS + codex_delivery.SUBMIT_RPCS) * 10.0


def test_an_input_the_adapter_retries_is_not_reported_rejected(adapter):
    delivery, _ = adapter
    delivery._connect.side_effect = OSError('backend unreachable')
    receipt = delivery.deliver('bus text', client_id='s-1/000001', adapter_converges=True)
    assert receipt['state'] == 'retrying' and receipt['retryable']
    assert 'the adapter retries it' in receipt['note']


def test_a_steer_held_behind_a_running_turn_is_not_escalated(adapter, monkeypatch):
    delivery, thread = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    delivery.escalate = Mock(return_value='s-1/000009')
    thread['status'] = {'type': 'active', 'activeFlags': []}
    delivery.deliver('during a long tool call', client_id='s-1/000001', adapter_converges=True)
    for _ in range(4):
        clock[0] += codex_delivery.CONFIRM_DEADLINE
        delivery.refresh()
    delivery.escalate.assert_not_called()
    thread['status'] = {'type': 'idle'}
    delivery.refresh()
    clock[0] += codex_delivery.CONFIRM_DEADLINE - 1
    delivery.refresh()
    delivery.escalate.assert_not_called()
    clock[0] += 1
    delivery.refresh()
    delivery.escalate.assert_called_once()
    assert 'idle' in delivery.escalate.call_args.args[1]


def test_a_turn_that_never_ends_still_escalates_at_the_cap(adapter, monkeypatch):
    delivery, thread = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    delivery.escalate = Mock(return_value='s-1/000009')
    thread['status'] = {'type': 'active', 'activeFlags': []}
    delivery.deliver('never taken', client_id='s-1/000001', adapter_converges=True)
    clock[0] += codex_delivery.MAX_CONFIRM_WAIT - 1
    delivery.refresh()
    delivery.escalate.assert_not_called()
    clock[0] += 1
    delivery.refresh()
    delivery.escalate.assert_called_once()
    assert 'minutes' in delivery.escalate.call_args.args[1]


def test_an_escalation_that_fails_to_post_is_retried(adapter, monkeypatch):
    delivery, _ = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    delivery.escalate = Mock(side_effect=[OSError('bus write failed'), 's-1/000009'])
    original = delivery._request.side_effect
    delivery._request.side_effect = lambda method, params: (
        (_ for _ in ()).throw(codex_delivery.CodexDeliveryError('native rejection'))
        if method == 'turn/start' else original(method, params)
    )
    delivery.deliver('refused', client_id='s-1/000001', adapter_converges=True)
    delivery.refresh()
    assert delivery.escalate.call_count == 1
    assert not delivery.snapshot()['receipts'][0].get('escalated')
    delivery.refresh()
    assert delivery.escalate.call_count == 1, 'not before the retry interval'
    clock[0] += codex_delivery.RETRY_INTERVAL
    delivery.refresh()
    delivery.refresh()
    assert delivery.escalate.call_count == 2
    assert delivery.snapshot()['receipts'][0]['escalation'] == 's-1/000009'


@pytest.mark.parametrize('recovers', [True, False])
def test_startup_discovery_that_times_out_still_converges(adapter, monkeypatch, recovers):
    delivery, _ = adapter
    for name, value in (('START_TIMEOUT', .05), ('PENDING_INTERVAL', .01), ('RETRY_INTERVAL', .01),
                        ('CONFIRM_DEADLINE', .3)):
        monkeypatch.setattr(codex_delivery, name, value)
    delivery.escalate = Mock(return_value='s-1/000009')
    original = delivery._request.side_effect
    down = [True]

    def request(method, params):
        if method == 'thread/loaded/list' and down[0]:
            raise TimeoutError('discovery timed out')
        return original(method, params)

    delivery._request.side_effect = request
    started = threading.Event()
    delivery.start_monitor()
    try:
        worker = delivery.auto_start({'start_command': '/start'}, threading.Event(), started.set)
        for _ in range(500):
            if any('could not identify' in call.args[0] for call in delivery.notify.call_args_list):
                break
            threading.Event().wait(.01)
        down[0] = not recovers
        worker.join(timeout=10)
        assert started.is_set(), 'inbound is released either way'
        if recovers:
            assert [call.args[1]['clientUserMessageId'] for call in starts(delivery)] == ['startup']
            delivery.escalate.assert_not_called()
        else:
            assert starts(delivery) == []
            delivery.escalate.assert_called_once()
    finally:
        delivery._stop.set()
        delivery._monitor.join(timeout=5)


def test_health_answers_while_a_tick_holds_the_adapter(adapter):
    delivery, _ = adapter
    delivery.deliver('pending', client_id='message-1')
    held, done = threading.Event(), threading.Event()

    def tick():
        with delivery._lock:
            held.set()
            done.wait(5)

    holder = threading.Thread(target=tick)
    holder.start()
    held.wait(5)
    try:
        answered = []
        reader = threading.Thread(target=lambda: answered.append(delivery.snapshot()))
        reader.start()
        reader.join(timeout=1)
        assert answered and answered[0]['receipts'][0]['delivery_id'] == 'message-1'
    finally:
        done.set()
        holder.join()


def test_a_timed_out_rpc_is_counted_as_a_timeout():
    delivery = codex_delivery.CodexDelivery(['codex'], notify=Mock())
    delivery.rpc_timeout = .01
    delivery._ws = Mock()
    delivery._ws.recv.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        delivery._request('thread/loaded/list', {})
    assert delivery.snapshot()['rpc_latency']['timeouts'] == 1


@pytest.mark.parametrize('closed', [False, True])
def test_a_record_the_adapter_refuses_is_raised_and_the_next_still_goes(adapter, tmp_path, closed):
    """Its poster holds only ``queued``; a closed adapter means the session is ending."""
    delivery, _ = adapter
    log = comms.MessageLog('s-1', path=str(tmp_path / 'bus.jsonl'))
    delivery.escalate = functools.partial(supervisor._escalate_codex_delivery, log)
    try:
        first, _ = log.append(kind=comms.KIND_MESSAGE, text='one', source=comms.Source(comms.CHANNEL_PLATFORM))
        second, _ = log.append(kind=comms.KIND_MESSAGE, text='two', source=comms.Source(comms.CHANNEL_PLATFORM))
        real = delivery.deliver

        def deliver(text, **kwargs):
            if kwargs['client_id'] == first.id:
                raise codex_delivery.CodexDeliveryError('Cannot persist original Codex input')
            return real(text, **kwargs)

        delivery.deliver = deliver
        delivery._closed = closed
        relay = supervisor._InboundRelay(log, Mock(), 'codex', codex_delivery=delivery)
        refused, following = relay.enqueue(first), relay.enqueue(second)
        relay.flush()
        assert 'persist' in refused['error'] and refused['bytes_written'] == 0
        questions = [state.question for state in log.questions()]
        noted = any(first.id in call.args[0] for call in delivery.notify.call_args_list)
        if closed:
            assert questions == [] and noted
        else:
            assert [question.text for question in questions] == [
                f'The session may not have seen {first.id} (“one”): the adapter refused it '
                '(Cannot persist original Codex input). It was not resent. Answering relays your '
                'answer to the session; tell the sender to repeat it if the reply shows it was missed.']
            assert comms.off_thread(questions[0].to_dict()) and not noted
            assert following['state'] == 'accepted'
    finally:
        log.close()


def test_a_persist_failure_after_codex_accepted_is_not_a_refusal(adapter, tmp_path):
    """The submission happened; only the outbox copy is stale until the next write."""
    delivery, _ = adapter
    log = comms.MessageLog('s-1', path=str(tmp_path / 'bus.jsonl'))
    delivery.escalate = functools.partial(supervisor._escalate_codex_delivery, log)
    try:
        record, _ = log.append(kind=comms.KIND_MESSAGE, text='one', source=comms.Source(comms.CHANNEL_PLATFORM))
        persist, writes = delivery._persist, []

        def full_after_the_first_write():
            writes.append(1)
            if len(writes) > 1:
                raise OSError(28, 'No space left on device')
            persist()

        delivery._persist = full_after_the_first_write
        relay = supervisor._InboundRelay(log, Mock(), 'codex', codex_delivery=delivery)
        relay.enqueue(record)
        relay.flush()
        assert log.questions() == []
        assert [r['state'] for r in delivery.snapshot()['receipts']] == ['accepted']
        assert any('No space left' in call.args[0] for call in delivery.notify.call_args_list)
    finally:
        log.close()


def test_a_poster_is_told_queued_even_when_a_running_worker_rejects_at_once(adapter, tmp_path, monkeypatch):
    delivery, _ = adapter
    original = delivery._request.side_effect

    def refuse(method, params):
        if method == 'turn/start':
            raise codex_delivery.CodexDeliveryError('Codex refused the turn')
        return original(method, params)

    delivery._request.side_effect = refuse
    log = comms.MessageLog('route-session', path=str(tmp_path / 'bus.jsonl'))
    relay = supervisor._InboundRelay(log, Mock(), 'codex', codex_delivery=delivery)
    lock, enqueue, race = relay.lock, relay.enqueue, {}

    class WorkerFirst:
        """The relay lock, except that the poster's next take waits for the worker to finish."""

        def __enter__(self):
            if race and threading.current_thread() is race['poster']:
                race.pop('worker').join(10)
                race.clear()
            return lock.__enter__()

        def __exit__(self, *exc):
            return lock.__exit__(*exc)

    def enqueue_beside_a_running_worker(message):
        receipt = enqueue(message)
        race.update(worker=threading.Thread(target=relay.flush), poster=threading.current_thread())
        race['worker'].start()
        return receipt

    relay.lock = WorkerFirst()
    monkeypatch.setattr(relay, 'enqueue', enqueue_beside_a_running_worker)
    client = TestClient(supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), Mock(), 'secret', comms=log, codex_delivery=delivery, inbound_relay=relay,
    ), raise_server_exceptions=False)
    try:
        response = client.post('/messages', headers={'Authorization': 'Bearer secret'}, json={
            'kind': 'message', 'text': 'first', 'source': {'channel': 'platform'}})
        assert response.json()['relay'] == {'queued': True, 'bytes_written': 0}
        assert settled(delivery, response.json()['message']['id'])['state'] == 'rejected'
    finally:
        client.close()
        log.close()


def test_a_pty_relay_that_raises_is_not_reported_sent(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError('relay broke')

    monkeypatch.setattr(supervisor, '_relay_message', broken)
    log = comms.MessageLog('route-session', path=str(tmp_path / 'bus.jsonl'))
    client = TestClient(supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), Mock(), 'secret', comms=log, submit=Mock(return_value=(1, 'ok')),
    ), raise_server_exceptions=False)
    try:
        response = client.post('/messages', headers={'Authorization': 'Bearer secret'}, json={
            'kind': 'message', 'text': 'first', 'source': {'channel': 'platform'}})
        assert response.status_code == 500
    finally:
        client.close()
        log.close()


def test_an_idle_thread_escalates_even_while_paging_fails(adapter, monkeypatch):
    delivery, thread = adapter
    clock = [100.0]
    monkeypatch.setattr(codex_delivery.time, 'monotonic', lambda: clock[0])
    delivery.escalate = Mock(return_value='s-1/000009')
    thread['status'] = {'type': 'idle'}
    original = delivery._request.side_effect
    delivery._request.side_effect = lambda method, params: (
        (_ for _ in ()).throw(TimeoutError('paging timed out'))
        if method == 'thread/turns/list' else original(method, params)
    )
    delivery.deliver('unobservable', client_id='s-1/000001', adapter_converges=True)
    delivery.refresh()
    clock[0] += codex_delivery.CONFIRM_DEADLINE
    delivery.refresh()
    delivery.escalate.assert_called_once()
