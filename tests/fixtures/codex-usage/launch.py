"""Container stand-in: run the real supervisor against a read-only native fake."""
import json
import os
from pathlib import Path
import sys

from lmer_cli import codex_delivery, supervisor

root = Path(os.environ['CODEX_USAGE_PROBE_DIR'])
args = sys.argv[1:]
harness = args[args.index('--harness') + 1]
assert harness == 'codex'
mounts = [args[i + 1] for i, arg in enumerate(args) if arg == '--mount-dir']
status_mount = next(value for value in mounts if value.split(':')[1] == os.environ['LMER_STATUS_DIR'])
(root / 'launch.json').write_text(json.dumps({
    'harness': harness, 'status_env': os.environ['LMER_STATUS_DIR'],
    'mount': status_mount, 'no_repo': os.environ.get('LMER_NO_REPO'),
}))
os.environ['LMER_STATUS_DIR'] = status_mount.split(':')[0]
os.environ['LMER_HARNESS'] = harness


def start(self):
    (root / 'adapter-started').touch()
    return [sys.executable, '-c',
            'import os,time\nfrom pathlib import Path\n'
            'p=Path(os.environ["CODEX_USAGE_PROBE_DIR"])/"stop"\n'
            'deadline=time.monotonic()+90\n'
            'while not p.exists() and time.monotonic()<deadline: time.sleep(.05)\n']


def request(self, method, params):
    if method == 'thread/loaded/list':
        return {'data': ['native-launch']}
    if method == 'thread/read':
        return {'thread': {'id': 'native-launch', 'threadSource': 'user',
                           'path': str(root / 'token-count.jsonl'), 'cliVersion': '0.154.0',
                           'model': 'model-runtime', 'reasoningEffort': 'high'}}
    if method == 'account/rateLimits/read':
        (root / 'quota-requested').touch()
        return json.loads((root / 'account.json').read_text())
    raise AssertionError(method)


codex_delivery.CodexDelivery.start = start
codex_delivery.CodexDelivery._connect = lambda self: None
codex_delivery.CodexDelivery._request = request
options = {'fastapi': False, 'manual_start': True, 'host': '127.0.0.1',
           'port_range': (8700, 8799), 'token': '', 'auto_start_delay': .1,
           'auto_start_nudge_delay': .05, 'auto_start_ready_marker': '',
           'auto_start_ready_timeout': 0, 'auto_start_settle_delay': 0,
           'winsize_recheck_delay': 0, 'start_prompt': '', 'start_prompt_delay': 0}
stdin_read, stdin_write = os.pipe()
with open(os.devnull, 'wb') as output:
    try:
        result = supervisor.run_supervisor(['codex'], options, stdin_fd=stdin_read, stdout_fd=output.fileno())
    finally:
        os.close(stdin_read)
        os.close(stdin_write)
sys.exit(result)
