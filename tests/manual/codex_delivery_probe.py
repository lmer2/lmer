"""Manual installed-Codex proof; run from the repository root. See docs/CODEX-DELIVERY.md."""
import logging
import secrets
import fcntl, gzip, json, os, pty, select, signal, struct, subprocess, tempfile, termios, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
logging.basicConfig(level=logging.INFO, format='%(message)s')

def report(*parts, **_):
    logging.info(' '.join((str(part) for part in parts)))
root = Path(tempfile.mkdtemp(prefix='450-backend-'))
home = root / 'codex'
home.mkdir()
report('ROOT', root, flush=True)
requests = []
controls = {name: (threading.Event(), threading.Event(), threading.Event()) for name in ('stale', 'retry', 'held')}

def user_text(body):
    return [''.join((c.get('text', '') for c in m.get('content', []) if isinstance(c, dict))) for m in body.get('input', []) if m.get('role') == 'user']

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
        body = json.loads(raw)
        requests.append(body)
        text = user_text(body)[-1]
        rid = 'r' + str(len(requests))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()

        def event(v):
            self.wfile.write(('data: ' + json.dumps(v) + '\n\n').encode())
            self.wfile.flush()
        event({'type': 'response.created', 'response': {'id': rid}})
        if text == 'race-seed-wait' and (not any((m.get('type') == 'function_call_output' and m.get('call_id') == 'wait-call' for m in body.get('input', [])))):
            event({'type': 'response.output_item.done', 'output_index': 0, 'item': {'type': 'function_call', 'call_id': 'wait-call', 'name': 'exec_command', 'arguments': json.dumps({'cmd': 'lmer-signal ask --timeout 60 --interval 1 "native wait question"', 'yield_time_ms': 1000})}})
            event({'type': 'response.completed', 'response': {'id': rid, 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})
            return
        if text == 'race-seed-approval':
            event({'type': 'response.output_item.done', 'output_index': 0, 'item': {'type': 'function_call', 'call_id': 'approval-call', 'name': 'exec_command', 'arguments': json.dumps({'cmd': 'touch ' + str(root / 'APPROVED'), 'sandbox_permissions': 'require_escalated', 'justification': 'Isolated probe: explicit approval required'})}})
            event({'type': 'response.completed', 'response': {'id': rid, 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})
            return
        if text.startswith('race-seed-') and text.removeprefix('race-seed-') in controls:
            received, menu, release = controls[text.removeprefix('race-seed-')]
            received.set()
            if not menu.wait(180):
                return
            event({'type': 'response.output_text.delta', 'delta': '', 'safety_buffering': {'use_cases': ['cyber'], 'reasons': ['policy-check'], 'retry_model': 'gpt-5.6-luna'}})
            if not release.wait(180):
                return
        event({'type': 'response.output_item.done', 'output_index': 0, 'item': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'id': rid + 'msg', 'content': [{'type': 'output_text', 'text': 'Probe finished.'}]}})
        event({'type': 'response.completed', 'response': {'id': rid, 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
(home / 'config.toml').write_text(f'check_for_update_on_startup=false\nmodel="gpt-6-astra"\nmodel_reasoning_effort="high"\nmodel_provider="probe"\n[model_providers.probe]\nname="probe"\nbase_url="http://127.0.0.1:{server.server_port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\n[features]\nenable_request_compression=false\nhooks=true\n[projects."{root}"]\ntrust_level="trusted"\n')
env = {k: v for k, v in os.environ.items() if not k.startswith(('LMER_', 'CODEX_')) and (not any((x in k for x in ('TOKEN', 'API_KEY'))))}
env.update(CODEX_HOME=str(home), TERM='xterm-256color', LMER_RUN_STATE_GUARD='0')
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 160, 0, 0))
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from lmer_cli.codex_delivery import CodexDelivery
os.environ.clear()
os.environ.update(env)
command = ['codex', '--model', 'gpt-6-astra', '-c', 'model_reasoning_effort=high', '--no-alt-screen', '--dangerously-bypass-approvals-and-sandbox', '-C', str(root)]
adapter = CodexDelivery(command, notify=lambda text: report('ADAPTER NOTICE', text, flush=True))
from lmer_cli import supervisor, comms
from fastapi.testclient import TestClient
import socket
buslog = comms.MessageLog('native-probe')
probe_control = secrets.token_hex(24)
probe_agent = secrets.token_hex(24)

def refuse_terminal(*args):
    raise AssertionError('automatic delivery wrote terminal bytes')
app = supervisor._build_fastapi_app(supervisor.OutputBuffer(), refuse_terminal, probe_control, agent_token=probe_agent, comms=buslog, codex_delivery=adapter)
http = TestClient(app)
auth = {'Authorization': 'Bearer ' + probe_control}
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    apiport = listener.getsockname()[1]
api_thread, api_stop = supervisor._start_fastapi_server(app, '127.0.0.1', apiport)
os.environ.update(LMER_FASTAPI_PORT=str(apiport), LMER_FASTAPI_TOKEN=probe_agent, LMER_HARNESS='codex')
env.update(LMER_FASTAPI_PORT=str(apiport), LMER_FASTAPI_TOKEN=probe_agent, LMER_HARNESS='codex')
tui_command = adapter.start()
sock = adapter.socket
backend = adapter.backend
report('PRODUCTION ADAPTER BACKEND READY', flush=True)
proc = subprocess.Popen(tui_command, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
os.close(slave)
output = bytearray()

def drain(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if select.select([master], [], [], 0.1)[0]:
            data = os.read(master, 65536)
            output.extend(data)
            if b'\x1b[6n' in data:
                os.write(master, b'\x1b[1;1R')

def wait_for(check, label, seconds=40):
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        drain(0.2)
    assert check(), label
    report('OBSERVED', label, flush=True)

def records():
    result = []
    for p in home.glob('sessions/**/*.jsonl'):
        for line in p.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result

def user_records():
    return [''.join((c.get('text', '') for c in r['payload'].get('content', []) if isinstance(c, dict))) for r in records() if r.get('type') == 'response_item' and r.get('payload', {}).get('role') == 'user']

def completions():
    return sum((r.get('type') == 'event_msg' and r.get('payload', {}).get('type') == 'task_complete' for r in records()))

def paste(s):
    os.write(master, b'\x1b[200~' + s.encode() + b'\x1b[201~')
    drain(0.3)

def submit():
    os.write(master, b'\r')
    drain(0.3)

def model_has(s):
    return any((s == t for r in requests for t in user_text(r)))

def menu_for(name):
    cursor = len(output)
    controls[name][1].set()
    wait_for(lambda: b'Retry with a faster model' in output[cursor:], 'exact fallback menu ' + name)

def finish(name):
    before = completions()
    controls[name][2].set()
    wait_for(lambda: completions() > before, 'turn naturally completed ' + name)
    drain(0.5)

def delivered(s):
    wait_for(lambda: model_has(s), 'model received ' + repr(s))
    wait_for(lambda: s in user_records(), 'transcript received ' + repr(s))
    assert user_records().count(s) == 1
from websockets.sync.client import unix_connect
ws = None
rpc_seq = 0
events = []

def connect():
    global ws
    ws = unix_connect(str(sock), uri='ws://localhost/rpc', compression=None, open_timeout=10, max_size=32 * 1024 * 1024)
    rpc('initialize', {'clientInfo': {'name': 'lmer_probe', 'version': '0'}, 'capabilities': {'experimentalApi': True}})
    ws.send(json.dumps({'method': 'initialized'}))

def rpc(method, params, allow_error=False):
    global rpc_seq
    rpc_seq += 1
    ident = rpc_seq
    ws.send(json.dumps({'id': ident, 'method': method, 'params': params}))
    while True:
        response = json.loads(ws.recv(timeout=15))
        if response.get('id') == ident and ('result' in response or 'error' in response):
            if 'error' in response:
                assert allow_error, response
                report('EXPECTED REJECTION', method, response['error'], flush=True)
                return response
            return response['result']
        events.append(response)

def thread():
    return rpc('thread/read', {'threadId': tid, 'includeTurns': True})['thread']

def send(text, cid, **extra):
    if extra:
        return rpc('turn/start', {'threadId': tid, 'clientUserMessageId': cid, 'input': [{'type': 'text', 'text': text, 'text_elements': []}], **extra})['turn']['id']
    response = http.post('/input', headers=auth, json={'data': text, 'append_newline': True, 'delivery_id': cid})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['state'] == 'accepted', receipt
    with adapter._lock:
        before = adapter._sequence
        duplicate = adapter.deliver(text, client_id=cid)
        assert adapter._sequence == before, 'duplicate delivery issued a native RPC'
    assert duplicate['state'] in ('accepted', 'confirmed'), duplicate
    for key in ('delivery_id', 'payload_sha256', 'payload_length', 'thread_id', 'turn_id', 'model', 'effort'):
        assert duplicate[key] == receipt[key], (key, receipt, duplicate)
    return receipt['turn_id']

def confirm(text, cid):
    delivered(text)
    found = [item for turn in thread()['turns'] for item in turn['items'] if item.get('clientId') == cid]
    assert len(found) == 1, found
    assert found[0]['content'] == [{'type': 'text', 'text': text, 'text_elements': []}], found
    adapter.refresh()
    receipt = next((r for r in adapter.snapshot()['receipts'] if r['delivery_id'] == cid))
    assert receipt['state'] == 'confirmed' and receipt['submit_confirmed'], receipt
    report('CONFIRMED original text/clientId', repr(text), cid, flush=True)

def bus_send(text, *, answer=False):
    body = {'kind': 'message', 'text': text,
            'source': {'channel': 'platform', 'ref': 'native-answer' if answer else 'native-message'}}
    expected = '[platform] ' + text
    if answer:
        question = http.post('/messages', headers=auth, json={
            'kind': 'question', 'text': 'unclaimed native question', 'source': {'channel': 'agent'},
        })
        assert question.status_code == 200 and question.json()['relay'] is None
        q = question.json()['message']
        body.update(kind='answer', in_reply_to=q['id'])
        expected = f"[platform] (answer to #{q['seq']} 'unclaimed native question') " + text
    response = http.post('/messages', headers=auth, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    message = buslog.get(result['message']['seq'])
    assert message.text == text
    assert message.id == f'{buslog.session}/{message.seq:06d}'
    assert result['relay']['delivery_id'] == message.id
    assert result['relay']['state'] in ('accepted', 'confirmed'), result
    duplicate = http.post('/messages', headers=auth, json=body)
    assert duplicate.status_code == 200 and duplicate.json()['relay'] is None
    return expected, message.id

def idle():
    wait_for(lambda: thread()['status']['type'] == 'idle', 'thread idle')
try:
    startup_cancel = threading.Event()
    startup_done = threading.Event()
    startup_worker = None
    os.environ.update(LMER_LLM_NAME='gpt-6-astra', LMER_REASONING_EFFORT='high')
    adapter.start_monitor()
    wait_for(lambda: (adapter.snapshot()['runtime'] or {}).get('model') == 'gpt-6-astra',
             'native runtime observed before any automated startup', seconds=180)
    assert not requests and not adapter.snapshot()['receipts']
    report('PASS manual startup observes native runtime without submitting input')
    startup_worker = adapter.auto_start({'start_command': 'race-seed-stale'}, startup_cancel, startup_done.set)
    wait_for(controls['stale'][0].is_set, 'active initial request', seconds=180)
    assert startup_done.wait(5), 'managed startup callback'
    files = list(home.glob('sessions/**/*.jsonl'))
    assert len(files) == 1
    tid = json.loads(files[0].read_text().splitlines()[0])['payload']['id']
    connect()
    initial = thread()
    assert initial['id'] == tid
    assert initial['model'] == 'gpt-6-astra' and initial['reasoningEffort'] == 'high'
    turnid = initial['turns'][-1]['id']
    controls['stale'][1].set()
    finish('stale')
    idle()
    assert startup_done.is_set()
    report('PASS native managed startup through structured adapter', flush=True)
    controls['stale'] = (threading.Event(), threading.Event(), threading.Event())
    paste('race-seed-stale')
    submit()
    wait_for(controls['stale'][0].is_set, 'TUI submitted request')
    turnid = thread()['turns'][-1]['id']
    paste('STALE DRAFT MUST REMAIN UNSENT')
    menu_for('stale')
    payload = 'ordinary payload 123 preserve me'
    submitted = send(payload, 'delivery-menu')
    assert submitted == turnid
    bus_payload, bus_id = bus_send('! original bus message\n/slash — café')
    answer_payload, answer_id = bus_send('unclaimed answer original — café\nline two', answer=True)
    finish('stale')
    confirm(payload, 'delivery-menu')
    confirm(bus_payload, bus_id)
    confirm(answer_payload, answer_id)
    idle()
    assert not model_has('STALE DRAFT MUST REMAIN UNSENT')
    report('PASS actual midturn delivery on same TUI/backend/thread with exact menu and stale draft', flush=True)
    report('PASS real /messages and unclaimed answer through exact menu; tagged originals and bus IDs', bus_id, answer_id)
    payload = '! literal idle text\n/slash should remain prose — café'
    nextturn = send(payload, 'delivery-idle')
    assert nextturn != turnid
    confirm(payload, 'delivery-idle')
    idle()
    ws.close()
    adapter._disconnect()
    assert adapter._ws is None
    connect()
    assert thread()['id'] == tid
    payload = 'reconnect retains original payload'
    send(payload, 'delivery-reconnect')
    confirm(payload, 'delivery-reconnect')
    idle()
    report('PASS idle submission and client reconnect', flush=True)
    send('race-seed-retry', 'delivery-seed-race')
    wait_for(controls['retry'][0].is_set, 'race request active')
    ended_turn = thread()['turns'][-1]['id']
    controls['retry'][1].set()
    drain(0.5)
    finish('retry')
    idle()
    payload = 'turn boundary preserve once'
    rejected = rpc('turn/steer', {'threadId': tid, 'expectedTurnId': ended_turn, 'clientUserMessageId': 'delivery-race', 'input': [{'type': 'text', 'text': payload, 'text_elements': []}]}, allow_error=True)
    assert 'error' in rejected and payload not in user_records()
    send(payload, 'delivery-race')
    confirm(payload, 'delivery-race')
    idle()
    report('PASS explicit stale-turn rejection then atomic start/steer delivered once', flush=True)
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=5)
    master2, slave2 = pty.openpty()
    fcntl.ioctl(slave2, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 160, 0, 0))
    os.close(master)
    master = master2
    proc = subprocess.Popen(['codex', '--remote', 'unix://' + str(sock), '--no-alt-screen', 'resume', tid], stdin=slave2, stdout=slave2, stderr=slave2, env=env, cwd=root, start_new_session=True)
    os.close(slave2)
    cursor = len(output)
    wait_for(lambda: b'gpt-6-astra' in output[cursor:], 'TUI resumed same backend/thread', seconds=120)
    assert thread()['id'] == tid
    payload = 'after TUI resume preserve payload'
    send(payload, 'delivery-resume')
    confirm(payload, 'delivery-resume')
    idle()
    report('PASS TUI restart/resume without a new thread', flush=True)
    cursor = len(output)
    send('race-seed-approval', 'delivery-approval-seed', approvalPolicy='on-request', sandboxPolicy={'type': 'readOnly'})
    wait_for(lambda: b'Would you like to run the following command?' in output[cursor:], 'unrelated native approval', seconds=45)
    before = len(requests)
    payload = 'answer while unrelated approval stays pending'
    send(payload, 'delivery-approval')
    drain(2)
    assert not (root / 'APPROVED').exists() and len(requests) == before
    assert thread()['status']['type'] == 'active'
    report('PASS delivery did not accept or dismiss unrelated approval', flush=True)
    assert ('touch ' + str(root / 'APPROVED')).encode() in output[cursor:]
    os.write(master, b'y')
    drain(1)
    confirm(payload, 'delivery-approval')
    idle()
    assert (root / 'APPROVED').exists()
    report('PASS payload reached model after separate test actor approved harmless marker command', flush=True)
    send('race-seed-wait', 'delivery-wait-seed', approvalPolicy='never', sandboxPolicy={'type': 'dangerFullAccess'})
    wait_for(lambda: any((q.question.text == 'native wait question' for q in buslog.questions())), 'native lmer-signal wait question')
    question = next((q.question for q in buslog.questions() if q.question.text == 'native wait question'))
    answer = 'answer original 123 café'
    response = http.post('/messages', headers=auth, json={'kind': 'answer', 'text': answer, 'in_reply_to': question.id, 'source': {'channel': 'platform'}})
    assert response.status_code == 200, response.text
    assert response.json()['relay'] is None, response.json()
    wait_for(lambda: any((answer in str(m.get('output', '')) for r in requests for m in r.get('input', []) if m.get('type') == 'function_call_output')), 'model received CLI wait answer')
    assert not any((answer == t for r in requests for t in user_text(r))), 'wait answer delivered twice as user message'
    idle()
    report('PASS real lmer-signal answer/wait preserved without model-input duplication', flush=True)
    contexts = [(r['payload'].get('model'), r['payload'].get('effort')) for r in records() if r.get('type') == 'turn_context']
    assert contexts and all((c == ('gpt-6-astra', 'high') for c in contexts)), contexts
    assert requests and all((r.get('model') == 'gpt-6-astra' and r.get('reasoning', {}).get('effort') == 'high' for r in requests))
    assert next((r['payload'] for r in records() if r.get('type') == 'turn_context'))['approval_policy'] == 'never'
    report('PASS all requests/contexts retained gpt-6-astra/high; thread', tid, flush=True)
    report('PASS no draft concatenation and no automatic payload retry', flush=True)
    wait_for(lambda: all(r['state'] == 'confirmed' for r in adapter.snapshot()['receipts']),
             'all receipts settled before human model change')
    notices = []
    adapter.notify = notices.append
    changed = 'explicit human model change — preserve this text'
    send(changed, 'human-model-change', model='gpt-5.6-luna', effort='low')
    delivered(changed)
    idle()
    wait_for(lambda: (adapter.snapshot()['runtime'] or {}).get('preset_mismatch') is True,
             'monitor reports explicit human model change', seconds=15)
    runtime = http.get('/healthz', headers=auth).json()['codex_delivery']['runtime']
    assert (runtime['model'], runtime['effort']) == ('gpt-5.6-luna', 'low'), runtime
    assert any('gpt-5.6-luna/low' in notice for notice in notices)
    payload = 'automated payload after intentional human change — café'
    send(payload, 'delivery-human-setting')
    confirm(payload, 'delivery-human-setting')
    idle()
    changed_requests = [r for r in requests if user_text(r)[-1] in (changed, payload)]
    assert changed_requests and all((r['model'], r['reasoning']['effort']) == ('gpt-5.6-luna', 'low') for r in changed_requests)
    latest_context = [r['payload'] for r in records() if r.get('type') == 'turn_context'][-1]
    assert (latest_context['model'], latest_context['effort']) == ('gpt-5.6-luna', 'low')
    report('PASS bounded runtime observation after settled receipts; intentional human model control retained')
finally:
    for _, menu, release in controls.values():
        menu.set()
        release.set()
    (root / 'terminal.log').write_bytes(output)
    (root / 'requests.json').write_text(json.dumps(requests, indent=2))
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    if ws is not None:
        ws.close()
    startup_cancel.set()
    if startup_worker is not None:
        startup_worker.join(timeout=5)
    adapter.close()
    api_stop()
    api_thread.join(timeout=5)
    assert not api_thread.is_alive()
    buslog.close()
    assert startup_worker is None or not startup_worker.is_alive()
    os.close(master)
    server.shutdown()
    server.server_close()
    report('CLEANUP TUI/backend reaped; mock server closed', flush=True)
