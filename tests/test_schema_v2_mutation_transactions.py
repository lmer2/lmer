"""A supported write must not slip inside another session's claim transaction.

The claim used to stash the checkout, and these barriers hooked that stash's
reference transaction. It now publishes an object built in a temporary index
and never touches the checkout, so the barrier is the ref update its fetch and
push still make: the same point in the same lock, with no stash to hook.
"""

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tests.test_schema_v2_git_transactions import wait_for_file
from tests.test_schema_v2_release_claims import claim_clones, git  # noqa: F401
from tests.test_schema_v2_sessions import session_tree, seed_launch  # noqa: F401
from tests.timing_budgets import BARRIER, CLI, QUIET
from work_repo import cli, run_state
from work_repo.transactions import OWNER_ENV, checkout_lock


@pytest.mark.parametrize("writer", ["report", "log"])
def test_a_concurrent_record_write_survives_a_claim_transaction(claim_clones, tmp_path, writer):
    origin, root, _ = claim_clones
    first, _, _ = seed_launch()
    first_env = dict(os.environ)
    _, directory, _ = seed_launch()
    assert cli.cmd_log("Original committed log entry", []) == 0
    second_env = dict(os.environ)
    sentinel = root / "unrelated.txt"
    sentinel.write_text("committed unrelated bytes\n")
    git(root, "add", "widgets", sentinel.name)
    git(root, "commit", "-m", "two committed executions")
    git(root, "push")
    sentinel.write_text("unrelated dirty bytes to preserve\n")

    ready, release, attempting, finished = [tmp_path / name for name in ("ready", "release", "attempting", "finished")]
    hook = root / ".git/hooks/reference-transaction"
    hook.write_text(f'''#!{sys.executable}
import sys, time
from pathlib import Path
changes = sys.stdin.read()
ready, release = Path({str(ready)!r}), Path({str(release)!r})
if sys.argv[1] == "committed" and " refs/remotes/" in changes and not ready.exists():
    ready.touch()
    until = time.monotonic() + {BARRIER}
    while not release.exists():
        assert time.monotonic() < until, "claim barrier timed out"
        time.sleep(.01)
''')
    hook.chmod(0o755)
    source = tmp_path / "new-report.md"
    payload = "Successful concurrent write must survive a claim transaction.\n"
    source.write_text(payload)
    args = ["report", "--file", str(source)] if writer == "report" else ["log", payload]
    script = tmp_path / "writer.py"
    script.write_text(f'''import sys
from pathlib import Path
from work_repo import cli
Path({str(attempting)!r}).touch()
sys.argv = ["work", *{args!r}]
rc = cli.main()
Path({str(finished)!r}).write_text(str(rc))
raise SystemExit(rc)
''')
    children = []
    claim_output, write_output = tmp_path / "claim.txt", tmp_path / "write.txt"
    with claim_output.open("w") as claim_stream, write_output.open("w") as write_stream:
        try:
            children.append(subprocess.Popen(
                [sys.executable, "-c", "from work_repo.cli import cmd_release_claim; raise SystemExit(cmd_release_claim())"],
                env=first_env, stdout=claim_stream, stderr=claim_stream,
            ))
            assert wait_for_file(ready), claim_output.read_text()
            children.append(subprocess.Popen(
                [sys.executable, str(script)], env=second_env,
                stdout=write_stream, stderr=write_stream,
            ))
            assert wait_for_file(attempting)
            # Before the fix the write returns zero during this window. A
            # correctly serialized writer waits for the claim to finish.
            wait_for_file(finished, QUIET)
            release.touch()
            assert children[0].wait(timeout=CLI) == 0, claim_output.read_text()
            assert children[1].wait(timeout=CLI) == 0, write_output.read_text()
        finally:
            release.touch()
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    assert finished.read_text() == "0"
    if writer == "report":
        outputs = list((directory / "reports").glob("*.md"))
        assert len(outputs) == 1, "a successful report vanished inside the claim transaction"
        assert outputs[0].read_text() == payload
    else:
        assert any(entry["message"] == payload for entry in yaml.safe_load((directory / "log.yaml").read_text())), "a successful tracked log update vanished"
    assert sentinel.read_text() == "unrelated dirty bytes to preserve\n"
    assert yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))["holder"] == first["uid"]
    assert cli.cmd_commit("publish preserved concurrent write") == 0
    if writer == "report":
        assert git(origin, "show", f"main:{outputs[0].relative_to(root)}") == payload
    else:
        assert payload.strip() in git(origin, "show", f"main:{directory.relative_to(root)}/log.yaml")


DIRECT_WRITES = {
    "new-record": "from work_repo import session_record; session_record.start_launch(); run_state.ensure_run()",
    "state": "state = run_state.load_state(directory); state['goal'] = 'new goal'; run_state.write_state(directory, state)",
    "event": "run_state.append_event(directory, 'test-write')",
    "ledger": "run_state.write_ledger(directory, {'schema': 1, 'tasks': {}})",
    "release": "release_run.record_version(directory, '1.2.3')",
    "logger": "get_logger().log('An independent direct helper write')",
    "rename": "state = run_state.load_state(directory); state['name'] = 'renamed'; run_state.freeze_run_dir(directory, state)",
    "specs": "specs_index.upsert_spec_link(directory / 'spec.md', 'test-spec')",
    "report-command": "raise SystemExit(cli.cmd_report(str(source)))",
    "artifact-command": "raise SystemExit(cli.cmd_artifact('result.md', str(source)))",
    "session-start-command": "raise SystemExit(cli.cmd_session_start())",
    "session-end-command": "raise SystemExit(cli.cmd_session_end())",
}


def record_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file() and '.git' not in path.parts}


@pytest.mark.parametrize("writer", DIRECT_WRITES)
@pytest.mark.parametrize("hook", [False, True], ids=["independent-writer", "inherited-hook"])
def test_supported_mutation_boundary_waits_or_refuses_before_writing(
    claim_clones, tmp_path, writer, hook
):
    _, root, _ = claim_clones
    _, directory, _ = seed_launch()
    (directory / 'spec.md').write_text('# Existing specification\n')
    git(root, 'add', 'widgets')
    git(root, 'commit', '-m', 'existing record')
    git(root, 'push')
    source = tmp_path / 'artifact.md'
    source.write_text('A concurrent artifact to preserve.\n')
    attempting = tmp_path / 'attempting'
    script = tmp_path / 'direct-writer.py'
    script.write_text(f'''from pathlib import Path
from work_repo import cli, run_state, release_run, specs_index, get_logger
directory, source = Path({str(directory)!r}), Path({str(source)!r})
Path({str(attempting)!r}).touch()
{DIRECT_WRITES[writer]}
''')
    original = record_snapshot(root)
    process = None
    output = tmp_path / 'output.txt'
    with output.open('w') as stream:
        try:
            with checkout_lock(root):
                env = dict(os.environ)
                if not hook:
                    env.pop(OWNER_ENV, None)
                process = subprocess.Popen([sys.executable, str(script)], env=env,
                                           stdout=stream, stderr=stream)
                assert wait_for_file(attempting)
                if hook:
                    code = process.wait(timeout=CLI)
                    # session-end preserves its documented hook-facing exit
                    # policy, but must explicitly warn that nothing was saved.
                    assert code != 0 or writer == 'session-end-command', output.read_text()
                    assert 'invoking process' in output.read_text(), output.read_text()
                else:
                    with pytest.raises(subprocess.TimeoutExpired):
                        process.wait(timeout=QUIET)
                assert record_snapshot(root) == original, f'{writer} mutated a locked checkout'
            if not hook:
                assert process.wait(timeout=CLI) == 0, output.read_text()
                assert record_snapshot(root) != original, f'{writer} did not run after the transaction ended'
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait()


def test_a_hook_inside_a_claim_transaction_refuses_before_mutating(claim_clones, tmp_path):
    _, root, _ = claim_clones
    _, directory, _ = seed_launch()
    sentinel = root / 'unrelated.txt'
    sentinel.write_text('base\n')
    git(root, 'add', 'widgets', sentinel.name)
    git(root, 'commit', '-m', 'existing record')
    git(root, 'push')
    sentinel.write_text('dirty\n')
    source, result = tmp_path / 'source.md', tmp_path / 'hook-result.txt'
    source.write_text('A hook must not write inside a claim transaction.\n')
    hook = root / '.git/hooks/reference-transaction'
    hook.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
from work_repo import cli
changes = sys.stdin.read()
result = Path({str(result)!r})
if sys.argv[1] == 'committed' and ' refs/remotes/' in changes and not result.exists():
    result.write_text(str(cli.cmd_report({str(source)!r})))
''')
    hook.chmod(0o755)
    process = subprocess.run(
        [sys.executable, '-c', 'from work_repo import cli; raise SystemExit(cli.cmd_release_claim())'],
        capture_output=True, text=True, timeout=CLI,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert result.read_text() == '1'
    assert not (directory / 'reports').exists()
    assert sentinel.read_text() == 'dirty\n'


def test_verify_external_command_can_use_supported_work_writers(claim_clones, tmp_path):
    _, _, _ = claim_clones
    _, directory, _ = seed_launch()
    source = tmp_path / 'verification.md'
    source.write_text('Verification child writes after setup releases the lock.\n')
    assert cli.cmd_verify('nested work writer', [
        sys.executable, '-c',
        f'from work_repo import cli; raise SystemExit(cli.cmd_report({str(source)!r}))',
    ]) == 0
    assert list((directory / 'reports').glob('*.md'))
    receipts = [event for event in run_state.read_events(directory, last_n=0) if event['type'] == 'verify']
    assert receipts[-1]['data']['exit_code'] == 0
