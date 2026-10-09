"""Run the launch path and simulate fresh container homes over its mounts."""
import errno
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from lmer_cli.cli import parse_dir_mount_specs
from lmer_platform import api, assistant, codex_config, config as cfg, registry, spawn, store
from tests.conftest import strip_lmer_env
from tests.test_harness_runners import run_harness_runner

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = '/home/developer/.codex'


@pytest.fixture
def launch(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(tmp_path / 'platform'))
    monkeypatch.setattr(assistant, '_require_taskdef', lambda: None)
    argv_file = tmp_path / 'argv.json'
    binary = tmp_path / 'lmer'
    binary.write_text(f'#!{sys.executable}\nimport json,sys\n'
                      f'open({str(argv_file)!r}, "w").write(json.dumps(sys.argv))\n')
    binary.chmod(0o700)
    config = cfg.load({'lmer_bin': str(binary)})

    def start(kind='worker', target='455', untracked=False):
        if kind == 'assistant':
            result = assistant.start(config, settings={'harness': 'codex'})
        else:
            result = spawn.spawn_session(config, spawn.SpawnRequest(
                taskdef='develop', harness='codex',
                target='.' if untracked else f'https://gitlab.example.com/group/project/-/work_items/{target}',
                no_repo=untracked,
            ))
        assert spawn.wait_for_exit_recorded(result.session_id, timeout=30)
        command = json.loads(argv_file.read_text())
        specs = [command[i + 1] for i, arg in enumerate(command) if arg == '--mount-dir']
        specs = [spec for spec in specs if spec.split(':')[1] == DESTINATION]
        matching = parse_dir_mount_specs(specs, '')
        assert len(matching) == 1, 'Codex config has no durable directory mount'
        assert matching[0].mode == 'rw'
        return matching[0].host

    return start


def provision(home, durable):
    home.mkdir()
    (home / '.codex').symlink_to(durable, target_is_directory=True)
    result = subprocess.run([
        'bash', '-c', 'source "$1"; harness_provision_config codex/config.toml "$HOME/.codex/config.toml"',
        'probe', str(ROOT / 'libexec/harness-common.sh'),
    ], env={'PATH': os.environ['PATH'], 'HOME': str(home), 'HARNESS_GLOBAL_DIR': str(ROOT),
            'LMER_WORK_AGENT_FILES_ROOT': str(home / 'absent')},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return home / '.codex/config.toml'


@pytest.mark.parametrize('kind', ['worker', 'assistant'])
def test_atomic_preference_survives_real_launch_spec_replacement(launch, tmp_path, kind):
    durable = launch(kind)
    old = provision(tmp_path / 'container-one', durable)
    assert tomllib.loads(old.read_text())['projects']['/workspace']['trust_level'] == 'trusted'
    replacement = old.with_suffix('.tmp')
    replacement.write_text('personality="pragmatic"\n[notice]\nhide_rate_limit_model_nudge=true\n')
    replacement.replace(old)
    saved = old.read_bytes()
    assert launch(kind) == durable
    assert (durable / 'config.toml').read_bytes() == saved
    fresh = provision(tmp_path / 'container-two', durable)
    assert fresh.read_bytes() == saved
    assert tomllib.loads(fresh.read_text())['notice']['hide_rate_limit_model_nudge'] is True


def test_independent_runs_cannot_overwrite_each_others_preferences(launch, tmp_path):
    first = launch(target='455')
    second = launch(target='456')
    assert first != second
    one = provision(tmp_path / 'one', first)
    two = provision(tmp_path / 'two', second)
    before = two.read_bytes()
    one.write_text('[notice]\nhide_rate_limit_model_nudge=true\n')
    assert two.read_bytes() == before


def test_unidentified_workers_do_not_share_a_writable_config(launch):
    assert launch(untracked=True) != launch(untracked=True)


@pytest.fixture
def live_api(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(tmp_path / 'platform'))
    monkeypatch.setattr(spawn, '_FAILED_PUBLICATIONS', {})
    binary = tmp_path / 'lmer'
    binary.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(300)\n')
    binary.chmod(0o700)
    config = cfg.load({'lmer_bin': str(binary), 'work_repo_mirror': str(tmp_path / 'work')})
    processes = []
    open_ptys = set()
    openpty = spawn.pty.openpty
    close_fd = os.close

    def record_pty():
        pair = openpty()
        open_ptys.update(pair)
        return pair

    def close(fd):
        close_fd(fd)
        open_ptys.discard(fd)

    monkeypatch.setattr(spawn.pty, 'openpty', record_pty)
    monkeypatch.setattr(os, 'close', close)
    popen = spawn.subprocess.Popen

    def record(command, **kwargs):
        process = popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(spawn.subprocess, 'Popen', record)
    client = TestClient(api.create_app(config, 'test-secret-value'), raise_server_exceptions=False)

    def post():
        return client.post('/api/sessions', auth=('', 'test-secret-value'), json={
            'taskdef': 'develop', 'harness': 'codex',
            'target': 'https://gitlab.example.com/group/project/-/issues/455',
        })

    post.open_ptys = open_ptys
    yield post, processes
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)
    for entry in registry.list_sessions(live_only=False):
        assert spawn.wait_for_exit_recorded(entry['id'], timeout=30)
        for thread in threading.enumerate():
            if thread.name == f"lmer-platform-drain-{entry['id']}":
                thread.join(timeout=30)
                assert not thread.is_alive()
    for fd in tuple(open_ptys):
        close(fd)


def test_concurrent_run_launches_allow_one_writer_and_later_replacement(live_api, monkeypatch):
    post, processes = live_api
    rendezvous = threading.Barrier(2)
    build = spawn._build_command

    def overlap(*args, **kwargs):
        command = build(*args, **kwargs)
        # Force the old check/register gap; serialization leaves only one entrant.
        try:
            rendezvous.wait(timeout=10)
        except threading.BrokenBarrierError:
            pass
        return command

    monkeypatch.setattr(spawn, '_build_command', overlap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    live = registry.list_sessions(live_only=True)
    assert len(live) == len(processes) == 1
    winner = next(response.json() for response in responses if response.status_code == 201)
    assert live[0]['id'] == winner['session_id']
    refused = next(response for response in responses if response.status_code == 409)
    assert live[0]['id'] in refused.json()['detail']
    command = winner['command']
    mounts = [command[i + 1] for i, arg in enumerate(command) if arg == '--mount-dir'
              and command[i + 1].split(':')[1] == DESTINATION]
    assert len(mounts) == 1
    target = Path(mounts[0].split(':')[0]) / 'config.toml'
    temporary = target.with_suffix('.tmp')
    temporary.write_text('[notice]\nhide_rate_limit_model_nudge=true\n')
    temporary.replace(target)
    saved = target.read_bytes()
    processes[0].terminate()
    assert spawn.wait_for_exit_recorded(live[0]['id'], timeout=30)
    replacement = post()
    assert replacement.status_code == 201
    assert replacement.json()['session_id'] != live[0]['id']
    assert mounts[0] in replacement.json()['command']
    assert target.read_bytes() == saved
    assert len(registry.list_sessions(live_only=True)) == 1


def test_failed_launch_releases_ownership_for_another_request(live_api, monkeypatch):
    post, processes = live_api
    popen = spawn.subprocess.Popen

    def fail(*args, **kwargs):
        raise OSError('test launch failure')

    monkeypatch.setattr(spawn.subprocess, 'Popen', fail)
    response = post()
    assert response.status_code == 400
    assert 'test launch failure' in response.json()['detail']
    assert not processes
    assert not registry.list_sessions(live_only=True)
    monkeypatch.setattr(spawn.subprocess, 'Popen', popen)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(post).result(timeout=30)
    assert response.status_code == 201
    assert len(processes) == len(registry.list_sessions(live_only=True)) == 1


@pytest.mark.parametrize('failure', ['registry', 'callback'])
def test_registry_publication_failure_reaps_writer_before_retry(live_api, monkeypatch, failure):
    post, processes = live_api
    replace = Path.replace
    hits = []

    def full_disk(source, target):
        if Path(target).parent == registry.sessions_dir():
            hits.append(Path(target))
            raise OSError(errno.ENOSPC, 'injected registry publication failure')
        return replace(source, target)

    original_spawn = api.spawn_session

    def publish(register, session_id, pid):
        register()
        hits.append(registry.session_path(session_id))
        raise store.StoreError('injected publication callback failure')

    with monkeypatch.context() as fault:
        if failure == 'registry':
            fault.setattr(Path, 'replace', full_disk)
        else:
            fault.setattr(api, 'spawn_session', lambda config, request: original_spawn(
                config, request, publish_registration=publish,
            ))
        response = post()
    assert response.status_code == 500
    assert len(hits) == len(processes) == 1
    failed = processes[0]
    assert failed.returncode is not None, 'unpublished child was not reaped'
    with pytest.raises(ChildProcessError):
        os.waitpid(failed.pid, os.WNOHANG)
    assert not post.open_ptys
    assert not registry.token_path(hits[0].stem).exists()
    assert not registry.list_sessions(live_only=False)
    command = failed.args
    mount = next(command[i + 1] for i, arg in enumerate(command) if arg == '--mount-dir'
                 and command[i + 1].split(':')[1] == DESTINATION)
    target = Path(mount.split(':')[0]) / 'config.toml'
    target.write_text('[notice]\nhide_rate_limit_model_nudge=true\n')
    saved = target.read_bytes()
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(post).result(timeout=30)
    assert retry.status_code == 201
    assert mount in retry.json()['command']
    assert target.read_bytes() == saved
    assert sum(process.poll() is None for process in processes) == 1
    assert len(registry.list_sessions(live_only=True)) == 1


@pytest.mark.parametrize('cleanup_fault', ['signal', 'token'])
def test_failed_publication_cleanup_blocks_new_launch_until_recovered(live_api, monkeypatch, cleanup_fault):
    post, processes = live_api
    original_spawn = api.spawn_session
    unlink = Path.unlink

    def fail_publication(register, session_id, pid):
        raise store.StoreError('injected publication failure')

    def denied_signal(*args):
        raise PermissionError(errno.EPERM, 'injected cleanup failure')

    def denied_token(path, *args, **kwargs):
        if path.suffix == '.token':
            raise PermissionError(errno.EACCES, 'injected cleanup failure')
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        if cleanup_fault == 'signal':
            fault.setattr(os, 'killpg', denied_signal)
        else:
            fault.setattr(Path, 'unlink', denied_token)
        with monkeypatch.context() as publication:
            publication.setattr(api, 'spawn_session', lambda config, request: original_spawn(
                config, request, publish_registration=fail_publication,
            ))
            failed = post()
        assert failed.status_code == 400
        assert 'new launches are blocked' in failed.json()['detail']
        assert len(processes) == len(spawn._FAILED_PUBLICATIONS) == 1
        assert (processes[0].returncode is None) == (cleanup_fault == 'signal')
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                refused = pool.submit(post).result(timeout=30)
            assert refused.status_code == 400
            assert 'new launches are blocked' in refused.json()['detail']
            assert len(processes) == 1
            os.fstat(fd)
        finally:
            os.close(fd)
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(post).result(timeout=30)
    assert retry.status_code == 201
    assert not spawn._FAILED_PUBLICATIONS
    assert processes[0].returncode is not None
    assert sum(process.poll() is None for process in processes) == 1
    assert len(registry.list_sessions(live_only=True)) == 1


def test_unpublished_process_group_is_killed_when_it_ignores_term(live_api, monkeypatch, tmp_path):
    post, processes = live_api
    ready = tmp_path / 'group-ready'
    (tmp_path / 'lmer').write_text(
        f'#!{sys.executable}\nimport os,signal,subprocess,sys,time\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(300)"])\n'
        f'open({str(ready)!r},"w").write(str(child.pid))\n'
        'time.sleep(300)\n'
    )
    replace = Path.replace
    hits = []

    def full_disk(source, target):
        if Path(target).parent == registry.sessions_dir():
            deadline = time.monotonic() + 30
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), 'fake launcher never created its process group'
            hits.append(Path(target))
            raise OSError(errno.ENOSPC, 'injected registry publication failure')
        return replace(source, target)

    monkeypatch.setattr(spawn, '_UNPUBLISHED_TERM_GRACE', 0)
    monkeypatch.setattr(Path, 'replace', full_disk)
    response = post()
    assert response.status_code == 500
    assert len(hits) == len(processes) == 1
    assert processes[0].returncode == -signal.SIGKILL
    descendant = int(ready.read_text())
    deadline = time.monotonic() + 30
    while registry.is_live({'pid': descendant}) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not registry.is_live({'pid': descendant})
    assert not post.open_ptys
    assert not registry.token_path(hits[0].stem).exists()
    assert not registry.list_sessions(live_only=False)


def test_unpublished_group_gets_term_grace_after_its_leader_exits(live_api, monkeypatch, tmp_path):
    post, processes = live_api
    ready = tmp_path / 'ready'
    stopped = tmp_path / 'stopped'
    child = tmp_path / 'runtime.py'
    child.write_text(
        'import signal,time\n'
        'def stop(*args):\n'
        '    time.sleep(0.3)\n'
        f'    open({str(stopped)!r}, "w").write("stop forwarded")\n'
        '    raise SystemExit(0)\n'
        'signal.signal(signal.SIGTERM, stop)\n'
        f'open({str(ready)!r}, "w").write("ready")\n'
        'time.sleep(300)\n'
    )
    (tmp_path / 'lmer').write_text(
        f'#!{sys.executable}\nimport subprocess,sys,time\n'
        f'subprocess.Popen([sys.executable, {str(child)!r}])\n'
        'time.sleep(300)\n'
    )
    replace = Path.replace
    hits = []

    def full_disk(source, target):
        if Path(target).parent == registry.sessions_dir():
            deadline = time.monotonic() + 30
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), 'runtime child never installed its TERM handler'
            hits.append(Path(target))
            raise OSError(errno.ENOSPC, 'injected registry publication failure')
        return replace(source, target)

    monkeypatch.setattr(Path, 'replace', full_disk)
    response = post()
    assert response.status_code == 500
    assert len(hits) == len(processes) == 1
    assert processes[0].returncode is not None
    assert stopped.read_text() == 'stop forwarded'
    assert not post.open_ptys
    assert not registry.token_path(hits[0].stem).exists()


@pytest.mark.parametrize('flag,destination', [
    ('--mount-dir', DESTINATION), ('--mount-file', DESTINATION + '/config.toml'),
])
def test_platform_rejects_a_caller_redirecting_the_config_mount(flag, destination):
    with pytest.raises(spawn.SpawnError):
        spawn._reject_reserved_args([flag, f'/tmp/other:{destination}:rw'])


def test_host_seed_is_only_copied_into_a_new_scope(launch, tmp_path, monkeypatch):
    seed = tmp_path / 'seed.toml'
    seed.write_text('personality="pragmatic"\n')
    monkeypatch.setenv(codex_config.SEED_ENV, str(seed))
    durable = launch()
    assert durable.stat().st_mode & 0o777 == 0o700
    target = durable / 'config.toml'
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_bytes() == seed.read_bytes()
    target.write_text('[notice]\nhide_rate_limit_model_nudge=true\n')
    saved = target.read_bytes()
    seed.write_text('personality="friendly"\n')
    assert launch() == durable
    assert target.read_bytes() == saved
    new_scope = launch(target='456')
    assert (new_scope / 'config.toml').read_bytes() == seed.read_bytes()


def test_an_unreadable_configured_seed_refuses_launch(launch, tmp_path, monkeypatch):
    monkeypatch.setenv(codex_config.SEED_ENV, str(tmp_path / 'missing.toml'))
    with pytest.raises(spawn.SpawnError, match='cannot prepare persistent Codex configuration'):
        launch()


def test_model_and_effort_launch_overrides_do_not_change_saved_config(launch, tmp_path):
    durable = launch()
    home = tmp_path / 'runner'
    target = provision(home, durable)
    target.write_text('model="saved-model"\nmodel_reasoning_effort="low"\n'
                      '[notice]\nhide_rate_limit_model_nudge=true\n')
    saved = target.read_bytes()
    intercept = tmp_path / 'intercept-exec.sh'
    intercept.write_text('exec() { printf "%s\\n" "$@" > '
                         + shlex.quote(str(home / 'argv.txt')) + '; }\n')
    _, argv, _ = run_harness_runner('codex', home, env={
        'LMER_LLM_NAME': 'launch-model', 'LMER_REASONING_EFFORT': 'high',
        'BASH_ENV': str(intercept),
    })
    assert argv[argv.index('--model') + 1] == 'launch-model'
    assert 'model_reasoning_effort=high' in argv
    assert target.read_bytes() == saved
