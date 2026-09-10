"""Resource selection and work-repo runner contracts for issue #454."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest
import yaml

from lmer_cli import gate_cache, gates, test_resources as resources
from tests.conftest import strip_lmer_env

REPO = Path(__file__).resolve().parents[1]
GIB = 1024**3
POLICY = {"min_cpus": 4, "min_memory_mib": 4096}


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv(gate_cache.CACHE_DIR_ENV, str(tmp_path / "cache"))


def hierarchy(tmp_path, unified=True, root="/", group="/job"):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    mount = tmp_path / "cgroup"
    leaf = mount / group.removeprefix(root).lstrip("/")
    leaf.mkdir(parents=True)
    (proc / "self/cgroup").write_text(
        f"0::{group}\n" if unified else f"2:cpu,memory:{group}\n")
    fs = "cgroup2 cgroup rw" if unified else "cgroup cgroup rw,cpu,memory"
    (proc / "self/mountinfo").write_text(f"1 0 0:1 {root} {mount} rw - {fs}\n")
    return proc, mount, leaf


def limits(directory, cpu="400000 100000", memory=str(4 * GIB), unified=True):
    if unified:
        (directory / "cpu.max").write_text(cpu)
        (directory / "memory.max").write_text(memory)
    else:
        quota, period = cpu.split()
        (directory / "cpu.cfs_quota_us").write_text(quota)
        (directory / "cpu.cfs_period_us").write_text(period)
        (directory / "memory.limit_in_bytes").write_text(memory)


@pytest.mark.parametrize("unified", [True, False])
def test_ancestor_limits_and_affinity(tmp_path, monkeypatch, unified):
    proc, mount, leaf = hierarchy(tmp_path, unified)
    limits(leaf, "800000 100000", str(8 * GIB), unified)
    limits(mount, "450000 100000", str(4 * GIB), unified)
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda _: set(range(16)))
    assert resources.read_limits(proc) == resources.Limits(4, 4 * GIB)
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda _: {0, 1})
    assert resources.read_limits(proc) == resources.Limits(2, 4 * GIB)


@pytest.mark.parametrize("cpu,memory,expected", [
    ("max 100000", "max", (None, None)),
    ("10000 100000", str(4 * GIB), (0, 4 * GIB)),
    ("bad", "bad", (None, None)),
    ("400000 0", "0", (None, None)),
])
def test_unknown_and_fractional_limits(tmp_path, monkeypatch, cpu, memory, expected):
    proc, _, leaf = hierarchy(tmp_path)
    limits(leaf, cpu, memory)
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda _: {0, 1, 2, 3})
    assert resources.read_limits(proc) == resources.Limits(*expected)


def test_missing_nonroot_mount_limits_are_unknown(tmp_path, monkeypatch):
    proc, mount, leaf = hierarchy(tmp_path, root="/container", group="/container/job")
    limits(leaf)
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda _: {0, 1, 2, 3})
    assert resources.read_limits(proc) == resources.Limits(None, None)
    limits(mount, "max 100000", "max")
    assert resources.read_limits(proc) == resources.Limits(4, 4 * GIB)


@pytest.mark.parametrize("config", [
    None, False, {}, {"workers": 4},
    {**POLICY, "min_cpus": True}, {**POLICY, "min_memory_mib": -1},
    {**POLICY, "protocol": []}, {**POLICY, "protocol": "unknown"}])
def test_invalid_policy_is_normal_runner(config):
    assert not resources.select_parallel(config).parallel


@pytest.mark.parametrize("allocation,eligible", [
    ((4, 4 * GIB), True), ((2, 4 * GIB), False), ((4, 4 * GIB - 1), False),
    ((None, 4 * GIB), False), ((4, None), False),
])
def test_configured_floor(monkeypatch, allocation, eligible):
    monkeypatch.setattr(resources, "read_limits", lambda: resources.Limits(*allocation))
    assert resources.select_parallel(POLICY).parallel is eligible


@pytest.mark.parametrize("value", ["1", "true", " YES "])
def test_explicit_serial_override(monkeypatch, value):
    monkeypatch.setenv(resources.SERIAL_ENV, value)
    probe = Mock(side_effect=AssertionError("must not probe when disabled"))
    monkeypatch.setattr(resources, "read_limits", probe)
    assert not resources.select_parallel(POLICY).parallel


@pytest.fixture
def gate(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    task, project = tmp_path / "task-info", tmp_path / "project-info"
    task.mkdir()
    project.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(gates, "task_info_dir", lambda: task)
    monkeypatch.setattr(gates, "project_info_dir", lambda: project)
    monkeypatch.setattr(resources, "read_limits", lambda: resources.Limits(4, 4 * GIB))
    instance = gates.GateSystem()
    return instance, task, project


def configure(directory, policy=POLICY, executable=True, filename="gate-check.yaml"):
    (directory / filename).write_text(yaml.safe_dump({"tests": {"parallel": policy}}))
    script = directory / "gate-check-run-tests-parallel.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755 if executable else 0o644)
    return script


def normal_runner(directory):
    script = directory / "gate-check-run-tests.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


def test_work_only_and_task_precedence(gate):
    instance, task, project = gate
    local = instance.project_root / ".lmer"
    local.mkdir()
    configure(local)
    assert instance._find_parallel_test_runner() is None
    project_script = configure(project)
    assert instance._find_parallel_test_runner() == (project_script, "opaque")
    (task / "gate-check.yaml").write_text("unrelated: true\n")
    assert instance._find_parallel_test_runner() == (project_script, "opaque")
    task_script = configure(task)
    assert instance._find_parallel_test_runner() == (task_script, "opaque")
    configure(local, False)
    assert instance._find_parallel_test_runner() == (task_script, "opaque")


@pytest.mark.parametrize("policy,executable", [
    (False, True), (None, True),
    ({**POLICY, "min_cpus": 8}, True), (POLICY, False)])
def test_task_refusal_stops_lower_opt_in(gate, policy, executable):
    instance, task, project = gate
    configure(project)
    configure(task, policy, executable)
    assert instance._find_parallel_test_runner() is None


def test_pair_cannot_borrow_lower_script(gate):
    instance, task, project = gate
    configure(project)
    configure(task).unlink()
    assert instance._find_parallel_test_runner() is None


def test_yaml_priority_and_yml_support(gate):
    instance, task, _ = gate
    script = configure(task, filename="gate-check.yml")
    assert instance._find_parallel_test_runner() == (script, "opaque")
    (task / "gate-check.yaml").write_text("tests: {parallel: false}\n")
    assert instance._find_parallel_test_runner() is None
    (task / "gate-check.yaml").write_text("[invalid\n")
    assert instance._find_parallel_test_runner() is None


@pytest.mark.parametrize("config,normal,diagnostic", [
    ("unrelated: true\n", False, ""),
    ("tests: {text_diff_subset: [tests/test_alpha.py]}\n", False, ""),
    ("{}\n", False, ""),
    ("tests: {parallel: false}\n", True, "disabled"),
    ("tests: [\n", True, "unreadable"),
    ("- tests: {parallel: false}\n", True, "root must be a mapping"),
    ("tests: [{parallel: false}]\n", True, "tests must be a mapping"),
    ("tests: null\n", True, "tests must be a mapping"),
    ("tests: false\n", True, "tests must be a mapping"),
    ("tests: disabled\n", True, "tests must be a mapping"),
    ("null\n", True, "root must be a mapping"),
    ("false\n", True, "root must be a mapping"),
])
def test_policy_shapes_through_work_lookup(tmp_path, monkeypatch, capsys, config, normal, diagnostic):
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.chdir(root)
    for name, value in {
        "LMER_WORK_REPO_PATH": str(tmp_path / "work"),
        "LMER_REPO_HOST": "gitlab.example.com",
        "LMER_REPO_PROJECT": "group/project", "LMER_TASK": "develop",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(resources, "read_limits", lambda: resources.Limits(4, 4 * GIB))
    task, project = gates.task_info_dir(), gates.project_info_dir()
    task.mkdir(parents=True)
    project.mkdir(parents=True)
    configure(project).write_text("#!/bin/sh\nprintf 'PROJECT_PARALLEL\\n'\n")
    normal_runner(task).write_text("#!/bin/sh\nprintf 'TASK_NORMAL\\n'\n")
    (task / "gate-check.yaml").write_text(config)
    instance = gates.GateSystem()
    result = instance.check_tests()
    assert result.status == gates.CheckStatus.PASSED
    assert result.full_output.strip() == ("TASK_NORMAL" if normal else "PROJECT_PARALLEL")
    assert diagnostic in capsys.readouterr().out
    instance.results = [result]
    assert instance.receipt_test_fields() == {}


@pytest.mark.parametrize("mode", ["timeout", "exit", "invalid", "success", "detached"])
def test_descriptor_collects_real_shell_interpreter_and_child(gate, monkeypatch, tmp_path, mode):
    """Real descriptor lifecycle; bounded sleeps are collected even on assertion failure."""
    instance, task, _ = gate
    script = configure(task, {**POLICY, "protocol": "argv-v1"})
    pids = tmp_path / "descriptor-pids.json"
    program = tmp_path / "descriptor.py"
    program.write_text(
        "import json, os, subprocess, sys, time\n"
        "request = json.load(sys.stdin)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'], "
        f"stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session={mode == 'detached'})\n"
        f"open({str(pids)!r}, 'w').write(json.dumps([os.getppid(), os.getpid(), child.pid]))\n"
        f"mode = {mode!r}\n"
        "if mode == 'timeout': time.sleep(90)\n"
        "if mode in ('exit', 'detached'): raise SystemExit('descriptor fixture failure')\n"
        "response = [] if mode == 'invalid' else "
        "{'argv': [sys.executable, '-c', \"print('1 passed')\"], 'targets': request['targets']}\n"
        "print(json.dumps(response))\n")
    script.write_text(f'#!/bin/sh\n"{sys.executable}" "{program}"\n')
    monkeypatch.setattr(instance, "_interpreter_can_import", lambda _: True)
    monkeypatch.setattr(instance, "_text_diff_fast_path", lambda: None)
    monkeypatch.setattr(gate_cache, "compute_fingerprint", lambda *args: None)
    describe = gates.gate_descriptor.describe
    monkeypatch.setattr(gates.gate_descriptor, "describe", lambda argv, **kwargs:
                        describe(argv, **{**kwargs, "timeout": 1}))
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
    started = time.monotonic()
    try:
        result = instance.check_tests()
        assert time.monotonic() - started < 8
        expected = gates.CheckStatus.PASSED if mode == "success" else gates.CheckStatus.FAILED
        assert result.status == expected
        assert pids.exists(), "the real interpreter must reach child creation"
        assert all(not Path(f"/proc/{pid}").exists() for pid in json.loads(pids.read_text()))
        assert unrelated.poll() is None
        if mode != "success":
            assert result.message == "Parallel test command description failed"
            assert instance._passing_suite is None
        if mode == "timeout":
            assert "timed out" in result.full_output
        if mode == "exit":
            assert "descriptor fixture failure" in result.full_output
    finally:
        unrelated.kill()
        unrelated.wait(timeout=2)
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("parallel,returncode", [(False, 0), (True, 0), (True, 1)])
def test_opaque_runner_keeps_command_and_receipt_contract(gate, monkeypatch, parallel, returncode):
    instance, task, project = gate
    normal_runner(project)
    expected = normal_runner(task)
    if parallel:
        expected = configure(project)
    run = Mock(return_value=subprocess.CompletedProcess([], returncode, "example output", ""))
    monkeypatch.setattr(gates.subprocess, "run", run)
    monkeypatch.setattr(gate_cache, "compute_fingerprint", Mock(side_effect=AssertionError("opaque cache")))
    result = instance.check_tests()
    assert result.status == (gates.CheckStatus.PASSED if returncode == 0 else gates.CheckStatus.FAILED)
    assert run.call_count == 1
    assert run.call_args.args == ([str(expected)],)
    assert run.call_args.kwargs == {"capture_output": True, "text": True, "cwd": instance.project_root}
    assert result.scope_targets is None
    instance.results = [result]
    assert instance.receipt_test_fields() == {}


def test_resource_fallback_retains_normal_custom_runner(gate, monkeypatch):
    instance, task, project = gate
    configure(project)
    expected = normal_runner(task)
    monkeypatch.setattr(resources, "read_limits", lambda: resources.Limits(None, None))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr(gates.subprocess, "run", run)
    assert instance.check_tests().status == gates.CheckStatus.PASSED
    assert run.call_args.args == ([str(expected)],)


def prepare_description(gate, monkeypatch, response=None, failed=False, subset=False):
    instance, task, _ = gate
    script = configure(task, {**POLICY, "protocol": "argv-v1"})
    monkeypatch.setattr(instance, "_interpreter_can_import", lambda _: True)
    targets = ["tests/test_alpha.py"] if subset else ["tests/"]
    fast = (["README.md"], targets, task / "gate-check.yaml") if subset else None
    monkeypatch.setattr(instance, "_text_diff_fast_path", lambda: fast)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv == [str(script), "--lmer-command"]:
            request = json.loads(kwargs["input"])
            payload = response if response is not None else {
                "argv": request["argv"] + ["-n", "4"], "targets": request["targets"]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "2 passed in 0.1s", "")
    monkeypatch.setattr(gates.subprocess, "run", run)
    monkeypatch.setattr(gates.gate_descriptor, "describe", run)
    monkeypatch.setattr(gate_cache, "compute_fingerprint", lambda *args: None)
    return instance, calls, targets


@pytest.mark.parametrize("subset", [False, True])
def test_described_command_scope_execution_and_passing_handoff(gate, monkeypatch, subset):
    instance, calls, targets = prepare_description(gate, monkeypatch, subset=subset)
    record = Mock(return_value="passing-suite-sentinel")
    monkeypatch.setattr(instance, "_record_passing_suite", record)
    result = instance.check_tests()
    assert result.status == gates.CheckStatus.PASSED
    assert result.scope_targets == targets
    assert (result.scope == gates.TEXT_DIFF_SCOPE) is subset
    assert calls[1][0][-2:] == ["-n", "4"]
    assert all(target in calls[1][0] for target in targets)
    assert calls[0][1]["timeout"] == 30
    assert record.call_args.args[1] == calls[1][0]
    assert record.call_args.args[2]["LMER_GATE_RUNNER_SHA256"]
    assert instance._passing_suite == "passing-suite-sentinel"
    instance.results = [result]
    assert instance.receipt_test_fields()["test_scope"] == ("text-diff subset" if subset else "full suite")


@pytest.mark.parametrize("response", [
    [], {}, {"argv": [], "targets": ["tests/"]},
    {"argv": ["python"], "targets": ["other/"]},
    {"argv": ["bad\0arg"], "targets": ["tests/"]}])
def test_bad_description_fails_without_suite_or_retry(gate, monkeypatch, response):
    instance, calls, _ = prepare_description(gate, monkeypatch, response=response)
    assert instance.check_tests().status == gates.CheckStatus.FAILED
    assert len(calls) == 1
    assert instance._passing_suite is None


def test_executed_description_failure_does_not_retry(gate, monkeypatch):
    instance, calls, _ = prepare_description(gate, monkeypatch, failed=True)
    assert instance.check_tests().status == gates.CheckStatus.FAILED
    assert len(calls) == 2
    assert instance._passing_suite is None


@pytest.fixture
def project_helper():
    spec = importlib.util.spec_from_file_location("project_gate_tests", REPO / "ci/gate-tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("cpus,present,expected", [
    (8, True, 4), (2, True, 2),
    (1, True, 0), (None, True, 0), (4, False, 0)])
def test_project_worker_cap_and_dependency_fallback(project_helper, monkeypatch, cpus, present, expected):
    helper = project_helper
    monkeypatch.setattr(helper, "read_limits", lambda: resources.Limits(cpus, 4 * GIB))
    monkeypatch.setattr(helper.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0 if present else 1)))
    argv = [sys.executable, "-m", "pytest", "tests/test_example.py", "-x"]
    assert helper.test_command(argv) == argv + (["-n", str(expected)] if expected else [])


def test_documented_work_only_contract():
    doc = (REPO / "docs/GATE-FASTPATH.md").read_text()
    for text in ("gate-check-run-tests-parallel.sh", "min_memory_mib", "argv-v1",
                 "LMER_GATE_NO_PARALLEL", "eligibility floor"):
        assert text in doc


@pytest.mark.parametrize("subset", [False, True])
def test_real_targets_cache_command_change_and_commit_handoff(gate, monkeypatch, tmp_path, subset):
    """Real git/cache/description processes; the fixture command never runs pytest."""
    instance, task, _ = gate
    root = instance.project_root
    counter = tmp_path / "executions"
    runner = root / "fixture_command.py"
    runner.write_text(
        "from pathlib import Path\n"
        f"p = Path({str(counter)!r})\n"
        "p.write_text(p.read_text() + 'run\\n' if p.exists() else 'run\\n')\n")
    (root / "tests/test_alpha.py").write_text("# target fixture\n")
    (root / "README.md").write_text("fixture\n")
    (root / ".lmer").mkdir()
    (root / ".lmer/gate-check.yaml").write_text(
        "tests:\n  text_diff_subset: [tests/test_alpha.py]\n")

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Gate fixture")
    git("add", "README.md", "fixture_command.py", "tests/test_alpha.py", ".lmer/gate-check.yaml")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    script = configure(task, {**POLICY, "protocol": "argv-v1"})

    def describe(workers):
        script.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            "request = json.load(sys.stdin)\n"
            f"argv = [{sys.executable!r}, {str(runner)!r}, *request['targets'], {str(workers)!r}]\n"
            "json.dump({'argv': argv, 'targets': request['targets']}, sys.stdout)\n")
    describe(4)
    if subset:
        (root / "README.md").write_text("updated fixture\n")
        git("add", "README.md")
    else:
        runner.write_text(runner.read_text() + "# staged source change\n")
        git("add", "fixture_command.py")
    instance.commit_handoff = True
    first = instance.check_tests()
    assert first.status == gates.CheckStatus.PASSED
    assert first.scope_targets == (["tests/test_alpha.py"] if subset else ["tests/"])
    second = instance.check_tests()
    assert second.cache_verdict == "hit"
    assert counter.read_text().splitlines() == ["run"]
    describe(2)
    third = instance.check_tests()
    assert third.cache_verdict != "hit"
    assert counter.read_text().splitlines() == ["run", "run"]
    suite = instance._passing_suite
    assert suite.argv[-1] == "2"
    git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture change")
    assert instance.handoff_test_cache_after_commit()
    fingerprint = gate_cache.compute_fingerprint(instance.run_command, suite.argv, suite.environment)
    assert gate_cache.read_pass(fingerprint)["argv"] == suite.argv
    after = instance.check_tests()
    assert after.scope_targets == ["tests/"]
    assert (after.cache_verdict == "hit") is not subset
    assert len(counter.read_text().splitlines()) == (3 if subset else 2)


def test_project_wrapper_isolates_nested_selection(project_helper, monkeypatch):
    helper = project_helper
    monkeypatch.setenv(resources.SERIAL_ENV, "0")
    monkeypatch.setattr(sys, "argv", ["gate-tests.py", "--run", "python3", "-m", "pytest", "-n", "4"])
    calls = []

    def execute(program, argv, environment):
        calls.append((program, argv, dict(environment)))
        raise SystemExit(0)
    monkeypatch.setattr(helper.os, "execvpe", execute)
    with pytest.raises(SystemExit) as stopped:
        helper.main()
    assert stopped.value.code == 0
    assert calls[0][1][-2:] == ["-n", "4"]
    assert calls[0][2][resources.SERIAL_ENV] == "1"


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(2, ["runner"], output="description failed", stderr="details"),
    subprocess.TimeoutExpired(["runner"], 30),
    OSError("runner vanished"),
])
def test_description_process_errors_remain_visible(gate, monkeypatch, error):
    instance, calls, _ = prepare_description(gate, monkeypatch)
    failed = Mock(side_effect=error)
    monkeypatch.setattr(gates.gate_descriptor, "describe", failed)
    result = instance.check_tests()
    assert result.status == gates.CheckStatus.FAILED
    assert result.full_output
    assert failed.call_count == 1
    assert instance._passing_suite is None
