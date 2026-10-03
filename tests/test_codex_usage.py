"""Representative native observations through delivery, platform, and rendered UI."""
from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from lmer_cli import codex_delivery, codex_usage
from lmer_platform import api, assistant, config as cfg, registry, spawn, statusline, store
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
    monkeypatch.setattr(codex_delivery.time, 'time', lambda: NOW.timestamp())
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
    assert served['context']['percent'] == 0
    assert served['context']['age_seconds'] == 600
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
            default = _text(_render(_props(status, compact=compact)))
            assert 'ctx' in default and '7d' in default and 'eff' in default
            assert 'tokens' not in default and 'Other model' not in default
            html = _render(_props(status, compact=compact), details=True)
            text = _text(html)
            assert '12,300 tokens' in text and '91,000 tokens' in text
            assert '258,400 tokens' in text
            assert '7d' in text and '15%' in text
            assert '5h' in text and '0%' in text
            assert 'account bucket other: Other model' in html
            assert 'account/rateLimits/read' in html and 'rollout/token_count' in html
            assert '2026-09-14T12:00:00Z' in html


def test_fresh_account_read_cannot_freshen_old_token_counts(observation):
    delivery, _, _, directory, _ = observation
    delivery.refresh()
    served = read(directory)
    text = _text(_render(_props(served, compact=True)))
    assert 'ctx 0% · 7d 15% · eff high (as of 10m ago)' in text
    assert text.count('as of') == 1
    assert 'tokens' not in text


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
    text = _text(_render(_props(served, compact=True), details=True))
    assert 'window unknown 23%' in text
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
        html = _render(_props(served, compact=compact))
        text = _text(html)
        assert '15%' in text and '% used' not in text
        assert 'reset passed; awaiting new snapshot' in html
        assert 'reset passed' not in text


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


@pytest.mark.parametrize('case', json.loads((FIXTURES / 'context-cases.json').read_text()))
def test_context_matches_codex_baseline_and_rounding(observation, case):
    delivery, _, _, directory, rollout = observation
    record = json.loads(rollout.read_text())
    record['payload']['info']['last_token_usage']['total_tokens'] = case['total']
    record['payload']['info']['model_context_window'] = case['window']
    rollout.write_text(json.dumps(record) + '\n')
    delivery.refresh()
    published = json.loads((directory / 'status.json').read_text())
    assert published['context']['percent'] == case['percent']
    served = read(directory)
    assert served['context']['percent'] == case['percent']
    assert served['context']['updated'] == served['codex']['tokens']['updated']
    assert served['context']['age_seconds'] == 600


@pytest.mark.parametrize('field', ['last_token_usage', 'model_context_window'])
@pytest.mark.parametrize('missing', [None, True, '258400', -1, 2 ** 54])
def test_missing_context_inputs_remove_previous_context(observation, field, missing):
    delivery, _, _, directory, rollout = observation
    delivery.refresh()
    assert 'context' in read(directory)
    record = json.loads(rollout.read_text())
    info = record['payload']['info']
    info[field] = {'total_tokens': missing} if field == 'last_token_usage' else missing
    rollout.write_text(json.dumps(record) + '\n')
    delivery._next_runtime_read = 0
    delivery.refresh()
    assert 'context' not in json.loads((directory / 'status.json').read_text())
    assert 'context' not in read(directory)


@pytest.mark.parametrize(('fixture', 'expected'), [
    ('account.json', [('7d', 15)]),
    ('account-two-windows.json', [('5h', 24), ('7d', 61)]),
    ('account-reversed-windows.json', [('5h', 24), ('7d', 61)]),
])
def test_main_account_is_chosen_by_id_and_only_reports_present_windows(observation, fixture, expected):
    delivery, _, account, directory, _ = observation
    account.clear()
    account.update(json.loads((FIXTURES / fixture).read_text()))
    buckets = account['rateLimitsByLimitId']
    account['rateLimitsByLimitId'] = {'other': buckets['other'], 'codex': buckets['codex']}
    delivery.refresh()
    published = json.loads((directory / 'status.json').read_text())
    published_main = [window for window in published['codex']['account']['limits']
                      if window['bucket'] == 'codex']
    assert [(window['period_label'], window['percent']) for window in published_main] == expected
    served = read(directory)
    windows = served['codex']['account']['limits']
    assert served['codex']['account']['main_bucket'] == 'codex'
    main = [window for window in windows if window['bucket'] == 'codex']
    assert [(window['period_label'], window['percent']) for window in main] == expected
    assert windows[:len(main)] == main
    text = _text(_render(_props(served, compact=True)))
    assert all(f'{label} {percent}%' in text for label, percent in expected)
    assert 'Other model' not in text and '0% used' not in text
    if len(expected) == 1:
        assert '5h' not in text
    else:
        assert '5h 24% · 7d 61%' in text
    expanded = _render(_props(served, compact=True), details=True)
    assert 'Other model' in expanded and '5h 0%' in _text(expanded)


def test_main_bucket_survives_the_window_bound():
    buckets = {f'special-{i}': {'primary': {'usedPercent': i}} for i in range(10)}
    buckets['codex'] = {'primary': {'usedPercent': 27, 'windowDurationMins': 10080}}
    account = codex_usage.account_snapshot({'rateLimitsByLimitId': buckets})
    assert len(account['limits']) == 8
    assert account['limits'][0]['bucket'] == account['main_bucket'] == 'codex'


@pytest.mark.parametrize('multiple', [False, True])
def test_single_bucket_fallback_does_not_guess_between_special_buckets(multiple):
    buckets = {'only': {'primary': {'usedPercent': 17, 'windowDurationMins': 300}}}
    if multiple:
        buckets['second'] = {'primary': {'usedPercent': 3}}
    account = codex_usage.account_snapshot({'rateLimitsByLimitId': buckets})
    assert account['main_bucket'] == (None if multiple else 'only')
    single = codex_usage.account_snapshot({'rateLimits': buckets['only']})
    assert single['main_bucket'] == 'codex'
    assert single['limits'][0]['period_label'] == '5h'


@pytest.mark.parametrize('change', [{'updated': 'invalid'}, {'thread_id': 'foreign'}, {'percent': None}])
def test_invalid_context_provenance_is_not_served(observation, change):
    delivery, _, _, directory, _ = observation
    delivery.refresh()
    path = directory / 'status.json'
    doc = json.loads(path.read_text())
    doc['context'].update(change)
    path.write_text(json.dumps(doc))
    assert 'context' not in read(directory)


def test_older_codex_document_still_renders_without_new_fields(observation):
    delivery, _, _, directory, _ = observation
    delivery.refresh()
    path = directory / 'status.json'
    doc = json.loads(path.read_text())
    doc.pop('context')
    doc['codex']['account'].pop('main_bucket')
    for window in doc['codex']['account']['limits']:
        window.pop('period_label')
    path.write_text(json.dumps(doc))
    text = _text(_render(_props(read(directory), compact=True)))
    assert '7d 15% · eff high' in text
    assert 'ctx' not in text and '5h' not in text


def test_codex_assistant_launch_starts_usage_monitor_and_serves_status(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(tmp_path / 'platform'))
    monkeypatch.setenv('CODEX_USAGE_PROBE_DIR', str(tmp_path))
    for name in ('token-count.jsonl', 'account.json'):
        shutil.copyfile(FIXTURES / name, tmp_path / name)
    fake_lmer = tmp_path / 'lmer'
    fake_lmer.write_text(f'#!{sys.executable}\n' + (FIXTURES / 'launch.py').read_text())
    fake_lmer.chmod(0o755)
    config = cfg.load({'lmer_bin': str(fake_lmer)})
    started = assistant.start(config, settings={'harness': 'codex'})
    try:
        deadline = time.monotonic() + 60
        path = statusline.status_file_for(started.session_id)
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(.05)
        assert path.is_file(), spawn.log_path_for(started.session_id).read_text()
        proof = json.loads((tmp_path / 'launch.json').read_text())
        assert proof['harness'] == 'codex'
        assert proof['status_env'] == statusline.CONTAINER_STATUS_DIR
        assert proof['mount'] == f'{path.parent}:{statusline.CONTAINER_STATUS_DIR}:rw'
        assert proof['no_repo'] == '1'
        assert (tmp_path / 'adapter-started').is_file()
        assert (tmp_path / 'quota-requested').is_file()
        assert json.loads(path.read_text())['context']['percent'] == 0
        client = TestClient(api.create_app(config, 'probe-secret'))
        headers = {'Authorization': 'Basic ' + base64.b64encode(b':probe-secret').decode()}
        reply = client.get('/api/assistant', headers=headers)
        assert reply.status_code == 200
        served = reply.json()['statusline']
        assert served['context']['percent'] == 0
        assert served['codex']['account']['main_bucket'] == 'codex'
        assert served['codex']['runtime']['effort'] == 'high'
        for compact in (True, False):
            text = _text(_render(_props(served, compact=compact)))
            assert 'ctx' in text and '7d' in text and 'high' in text
            assert '5h' not in text and 'tokens' not in text
    finally:
        (tmp_path / 'stop').touch()
        assert spawn.wait_for_exit_recorded(started.session_id, timeout=60)
