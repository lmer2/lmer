"""Representative native observations through delivery, platform, and rendered UI."""
from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from lmer_cli import codex_delivery, codex_usage
from lmer_platform import api, assistant, config as cfg, registry, statusline, store
from tests.conftest import strip_lmer_env
from tests.test_platform_web_statusline import _props, _render, _text

FIXTURES = Path(__file__).parent / 'fixtures/codex-usage'
NOW = datetime(2026, 9, 14, 12, 10, tzinfo=timezone.utc)


@pytest.fixture
def observation(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(tmp_path / 'platform'))
    directory = statusline.prepare_status_dir('s-codex')
    monkeypatch.setenv('LMER_STATUS_DIR', str(directory))
    monkeypatch.setattr(codex_usage, 'stamp', lambda: '2026-09-14T12:10:00Z')
    rollout = tmp_path / 'rollout.jsonl'
    rollout.write_bytes((FIXTURES / 'token-count.jsonl').read_bytes())
    thread = {'id': 'thread-one', 'threadSource': 'user', 'parentThreadId': None,
              'path': str(rollout), 'model': 'gpt-6-astra', 'reasoningEffort': 'high',
              'cliVersion': '0.154.0'}
    account = json.loads((FIXTURES / 'account.json').read_text())

    def request(method, params):
        if method == 'thread/loaded/list':
            return {'data': [thread['id']]}
        if method == 'thread/read':
            assert params['threadId'] == thread['id']
            return {'thread': thread}
        if method == 'account/rateLimits/read':
            return account
        raise AssertionError(method)

    delivery = codex_delivery.CodexDelivery(['codex'], notify=Mock())
    delivery._connect = Mock()
    delivery._disconnect = Mock()
    delivery._request = Mock(side_effect=request)
    yield delivery, thread, account, directory, rollout
    delivery._closed = True


def read(directory):
    return statusline.read_document(directory / 'status.json', now=NOW)


def test_native_usage_reaches_platform_routes_and_the_rendered_strip(observation, monkeypatch):
    delivery, thread, _, directory, _ = observation
    delivery.refresh()
    assert any(call.args[0] == 'account/rateLimits/read' for call in delivery._request.call_args_list)
    served = read(directory)
    assert served['codex']['tokens']['total']['total_tokens'] == 91000
    assert served['codex']['tokens']['age_seconds'] == 600
    assert served['codex']['account']['age_seconds'] == 0
    assert 'context' not in served
    registry.register('s-codex', pid=os.getpid(),
                      run={'host': 'example.com', 'project': 'project', 'slug': 'r1'},
                      task={'taskdef': 'develop', 'harness': 'codex'})
    monkeypatch.setattr(assistant, '_live_assistant', lambda: {'id': 's-codex', 'pid': os.getpid()})
    client = TestClient(api.create_app(cfg.load(), 'probe-secret'))
    headers = {'Authorization': 'Basic ' + base64.b64encode(b':probe-secret').decode()}
    fleet = client.get('/api/state', headers=headers).json()
    row = next(row for row in fleet['runs'] if row['slug'] == 'r1')
    assistant_reply = client.get('/api/assistant', headers=headers).json()
    for status in (row['statusline'], assistant_reply['statusline']):
        assert status['codex']['thread_id'] == thread['id']
        assert status['codex']['tokens']['updated'] == '2026-09-14T12:00:00Z'
        for compact in (True, False):
            html = _render(_props(status, compact=compact))
            text = _text(html)
            assert '12,300 tokens' in text and '91,000 tokens' in text
            assert '258,400 tokens' in text
            assert 'account codex 7d' in text
            assert 'account Other model 5h' in text and '0% used' in text
            assert 'account/rateLimits/read' in html and 'rollout/token_count' in html
            assert '2026-09-14T12:00:00Z' in html


def test_fresh_account_read_cannot_freshen_old_token_counts(observation):
    delivery, _, _, directory, _ = observation
    delivery.refresh()
    served = read(directory)
    text = _text(_render(_props(served, compact=True)))
    assert 'last response 12,300 tokens (as of 10m ago)' in text
    assert 'account codex 7d 15% used ·' in text


def test_reconnect_keeps_dates_and_failure_does_not_publish_zero(observation, monkeypatch):
    delivery, _, _, directory, rollout = observation
    delivery.refresh()
    original = read(directory)['codex']
    rollout.unlink()
    request = delivery._request.side_effect

    def disconnected_account(method, params):
        if method == 'account/rateLimits/read':
            raise TimeoutError('quota service unavailable')
        return request(method, params)

    delivery._request.side_effect = disconnected_account
    delivery._usage.next_account = delivery._next_runtime_read = 0
    monkeypatch.setattr(codex_usage, 'stamp', lambda: '2026-09-14T12:20:00Z')
    delivery.refresh()
    retained = read(directory)['codex']
    assert retained['tokens'] == original['tokens']
    assert retained['account'] == original['account']
    delivery._request.side_effect = request
    delivery._usage.next_account = delivery._next_runtime_read = 0
    delivery.refresh()
    assert read(directory)['codex']['tokens'] == original['tokens']
    assert read(directory)['codex']['account']['updated'] == '2026-09-14T12:20:00Z'


def test_new_assistant_session_cannot_reuse_previous_incarnations_usage(observation, monkeypatch):
    delivery, _, _, directory, _ = observation
    delivery.refresh()
    fresh = statusline.prepare_status_dir('s-replacement')
    monkeypatch.setenv('LMER_STATUS_DIR', str(fresh))
    replacement = codex_delivery.CodexDelivery(['codex'], notify=Mock())
    assert replacement._usage.tokens is None and replacement._usage.account is None
    assert read(fresh) is None
    assert read(directory) is not None


def test_missing_usage_and_unknown_window_never_become_zero(observation):
    delivery, _, account, directory, rollout = observation
    rollout.write_text('')
    account['rateLimitsByLimitId'] = {
        'unknown': {'primary': {'usedPercent': 23, 'windowDurationMins': None}},
        'missing': {'primary': {'usedPercent': None}},
    }
    delivery.refresh()
    served = read(directory)
    assert 'tokens' not in served['codex']
    text = _text(_render(_props(served, compact=True)))
    assert 'account unknown window unknown 23% used' in text
    assert '0%' not in text and 'tokens' not in text


@pytest.mark.parametrize('value', [None, True, '12', -1, 2 ** 54])
def test_malformed_counts_are_omitted(observation, value):
    delivery, _, _, directory, rollout = observation
    record = json.loads(rollout.read_text())
    info = record['payload']['info']
    info['last_token_usage'] = {'total_tokens': value}
    info['total_token_usage'] = None
    info['model_context_window'] = value
    rollout.write_text(json.dumps(record) + '\n')
    delivery.refresh()
    assert 'tokens' not in read(directory)['codex']


def test_wrong_thread_or_undated_reading_is_not_served(observation):
    delivery, _, _, directory, _ = observation
    delivery.refresh()
    path = directory / 'status.json'
    document = json.loads(path.read_text())
    document['codex']['tokens']['thread_id'] = 'other-thread'
    document['codex']['account']['updated'] = 'invalid'
    path.write_text(json.dumps(document))
    served = read(directory)['codex']
    assert 'tokens' not in served and 'account' not in served


def test_thread_change_refusal_cannot_relabel_retained_usage(observation):
    delivery, thread, _, directory, _ = observation
    delivery.refresh()
    before = (directory / 'status.json').read_bytes()
    thread.update(id='different-thread', model='different-model', reasoningEffort='low')
    delivery._next_runtime_read = 0
    delivery.refresh()
    assert (directory / 'status.json').read_bytes() == before


def test_polls_do_not_sum_account_usage_or_refetch_it_every_five_seconds(observation):
    delivery, _, _, directory, _ = observation
    delivery.refresh()
    for _ in range(3):
        delivery._next_runtime_read = 0
        delivery.refresh()
    assert sum(call.args[0] == 'account/rateLimits/read' for call in delivery._request.call_args_list) == 1
    windows = read(directory)['codex']['account']['limits']
    assert [window['percent'] for window in windows] == [15, 0, 35]


def test_expired_reset_is_not_presented_as_a_fresh_or_zero_quota(observation):
    delivery, _, account, directory, _ = observation
    account['rateLimitsByLimitId']['codex']['primary']['resetsAt'] = 1_700_000_000
    delivery.refresh()
    served = read(directory)
    assert served['codex']['account']['limits'][0]['reset_passed'] is True
    for compact in (True, False):
        text = _text(_render(_props(served, compact=compact)))
        assert '15% used' in text
        assert 'reset passed; awaiting new snapshot' in text


@pytest.mark.parametrize('percent', [None, True, '25', -1, float('nan'), float('inf'), 10 ** 1000])
def test_invalid_account_percentage_is_missing_not_zero(observation, percent):
    delivery, _, account, directory, _ = observation
    account['rateLimitsByLimitId'] = {'invalid': {'primary': {'usedPercent': percent}}}
    delivery.refresh()
    assert read(directory)['codex']['account']['limits'] == []


def test_oversized_rollout_tail_does_not_retimestamp_retained_tokens(observation):
    delivery, _, _, directory, rollout = observation
    delivery.refresh()
    before = read(directory)['codex']['tokens']
    with rollout.open('ab') as stream:
        stream.write(b'x' * (codex_usage.TOKEN_READ_BYTES + 1) + b'\n')
    delivery._next_runtime_read = 0
    delivery.refresh()
    assert read(directory)['codex']['tokens'] == before


def test_live_runtime_change_does_not_rewrite_token_provenance(observation):
    delivery, thread, _, directory, _ = observation
    delivery.refresh()
    before = read(directory)['codex']['tokens']
    thread.update(model='gpt-5.6-luna', reasoningEffort='low')
    delivery._next_runtime_read = 0
    delivery.refresh()
    served = read(directory)['codex']
    assert served['runtime']['model'] == 'gpt-5.6-luna'
    assert served['runtime']['effort'] == 'low'
    assert served['tokens'] == before


def test_rollout_reader_handles_torn_tail_and_refuses_nonregular_paths(tmp_path):
    path = tmp_path / 'rollout'
    sample = (FIXTURES / 'token-count.jsonl').read_bytes()
    path.write_bytes(sample + b'{"timestamp":')
    assert codex_usage.token_snapshot(path, 'one')['last']['total_tokens'] == 12300
    path.unlink()
    os.mkfifo(path)
    assert codex_usage.token_snapshot(path, 'one') is None


def test_no_status_mount_means_no_observation_requests(monkeypatch):
    monkeypatch.delenv('LMER_STATUS_DIR', raising=False)
    observer = codex_usage.CodexUsage()
    request = Mock(side_effect=AssertionError('No reader, no query'))
    observer.refresh({}, {}, request)
    request.assert_not_called()
