"""Delivery evidence is written by producers, scoped to the executing child."""
import json
import os
import shlex
import subprocess
import sys

import pytest

from lmer_cli import routing_receipts as receipts


def test_pretool_hook_scopes_parallel_children_and_preserves_shell_semantics(routing_call, tmp_path):
    from hooks.slack_reply_guard import main
    from io import StringIO
    from unittest.mock import patch

    session = "session ' $(must-not-run)"
    script = tmp_path / 'producer.py'
    script.write_text('from lmer_cli.routing_receipts import emit\nemit("post", "provider-ref")\n')
    original = f"printf '%s\\n' 'literal $value'; {shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    inputs = {'command': original, 'timeout': 30000, 'description': 'send reply'}

    def prepare(tool):
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
                   'session_id': session, 'tool_use_id': tool, 'tool_input': inputs}
        output = StringIO()
        with patch('sys.stdin', StringIO(json.dumps(payload))), patch('sys.stdout', output), \
                patch.dict(os.environ, {'LMER_CHAT_CHANNEL': 'matrix'}):
            assert main() == 0
        specific = json.loads(output.getvalue())['hookSpecificOutput']
        assert set(specific) == {'hookEventName', 'updatedInput'}
        assert specific['updatedInput']['timeout'] == inputs['timeout']
        assert specific['updatedInput']['description'] == inputs['description']
        return specific['updatedInput']['command']

    first, second = prepare('first'), prepare('second')
    result = subprocess.run(['bash', '-c', first], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'literal $value\n'
    assert receipts.delivered('first', session)
    assert not receipts.delivered('second', session)
    assert not receipts.delivered('first', 'another session')
    result = subprocess.run(['bash', '-c', second], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert receipts.delivered('second', session)


@pytest.mark.parametrize('kind', ['post', 'ask', 'dm'])
def test_only_confirmed_producers_write_body_free_evidence(routing_call, kind, tmp_path):
    assert not receipts.delivered('call', 'claude-test')
    receipts.record(kind, 'provider-ref')
    assert receipts.delivered('call', 'claude-test')
    [path] = (tmp_path / 'lmer-routing').glob('*.json')
    assert json.loads(path.read_text()) == {
        'session': 'claude-test', 'tool': 'call',
        receipts.KEY: {'version': 1, 'kind': kind, 'id': 'provider-ref'},
    }


def test_no_context_or_invalid_identity_cannot_make_evidence(routing_call, monkeypatch, tmp_path):
    for value in ['null', '[]', '{', '{}', '{"session":"s","tool":[]}']:
        monkeypatch.setenv(receipts.CONTEXT_ENV, value)
        receipts.record('post', 'provider-ref')
    assert not (tmp_path / 'lmer-routing').exists()


def test_local_record_failure_does_not_report_a_failed_send(routing_call, monkeypatch, capsys, caplog):
    def fail(*args, **kwargs):
        raise OSError('write refused')
    monkeypatch.setattr(receipts.os, 'replace', fail)
    receipts.emit('dm', 'provider-ref')
    assert 'write refused' in caplog.text
    assert json.loads(capsys.readouterr().err)[receipts.KEY]['id'] == 'provider-ref'
    assert not receipts.delivered('call', 'claude-test')


def test_corrupt_record_does_not_excuse_a_reply(routing_call):
    receipts.record('post', 'provider-ref')
    path = receipts._path('claude-test', 'call')
    for contents in ['{', 'null', '{}', '{"session": [], "tool": "call"}']:
        path.write_text(contents)
        assert not receipts.delivered('call', 'claude-test')


def test_records_do_not_cross_lmer_sessions(routing_call, monkeypatch):
    receipts.record('post', 'provider-ref')
    monkeypatch.setenv('LMER_SESSION_ID', 'another-lmer-session')
    assert not receipts.delivered('call', 'claude-test')
