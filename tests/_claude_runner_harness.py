"""Shared harness for tests that exercise libexec/claude-runner.sh.

Several test modules (reasoning effort, LLM name, human identity, agent
memory persistence) verify how claude-runner.sh translates ``LMER_*`` env
vars into ``claude`` CLI flags or side-effect commands. They all need the
same scaffolding: a stubbed ``claude`` binary that records its argv, a
minimal environment, and the subprocess invocation of the runner script.
This module centralizes that scaffolding so each test file only declares
the env vars it cares about.
"""
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest


CLAUDE_RUNNER = Path(__file__).parent.parent / "libexec" / "claude-runner.sh"

# claude-runner.sh unconditionally prepends /home/developer/.npm-global/bin to
# PATH (line ~4). If a real claude binary lives there it will shadow the stub
# this harness installs into a tmp dir, producing a confusing failure. Tests
# using the harness should gate themselves with this marker so they skip with
# a clear reason instead.
NPM_GLOBAL_CLAUDE = Path("/home/developer/.npm-global/bin/claude")
skip_if_npm_claude_present = pytest.mark.skipif(
    NPM_GLOBAL_CLAUDE.exists(),
    reason=(
        "Real claude at /home/developer/.npm-global/bin/claude would shadow "
        "the test stub because claude-runner.sh prepends that directory to PATH"
    ),
)


@dataclass
class ClaudeRunnerResult:
    """What claude-runner.sh did when invoked with the stubbed binaries."""

    output: str  # combined stdout+stderr of claude-runner.sh itself
    argv: list = field(default_factory=list)  # argv captured by the claude stub
    prompt: str | None = None  # contents of --append-system-prompt-file, if passed
    work_calls: list = field(default_factory=list)  # argv lines captured by the work stub


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_claude_runner(tmp_path, env=None, *, expose_python3=False, stub_work=False):
    """Run claude-runner.sh with a stubbed claude binary.

    The stub writes its argv to a file so callers can inspect what flags
    claude-runner.sh actually passes.

    Args:
        tmp_path: pytest tmp_path; hosts the fake bin dir, HOME, and capture files.
        env: extra env vars (typically the ``LMER_*`` var under test) layered
            on top of the minimal PATH/HOME environment.
        expose_python3: expose the test's Python interpreter as ``python3`` in
            the fake bin so renderer subprocesses find a Python with ``jinja2``
            available (the bare ``/usr/bin/python3`` in test environments may
            not have it). A wrapper script is used instead of a symlink to
            preserve PEP 405 venv detection (which inspects argv[0]'s location
            to find pyvenv.cfg).
        stub_work: also stub the ``work`` binary, appending each invocation's
            argv to a capture file (returned as ``work_calls``).
    """
    argv_file = tmp_path / "claude_argv.txt"
    work_calls_file = tmp_path / "work_calls.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        "exit 0\n"
    )
    _make_executable(fake_claude)

    if expose_python3:
        python_wrapper = fake_bin / "python3"
        python_wrapper.write_text(f'#!/bin/bash\nexec {sys.executable} "$@"\n')
        _make_executable(python_wrapper)

    if stub_work:
        fake_work = fake_bin / "work"
        fake_work.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$*" >> "{work_calls_file}"\n'
            "exit 0\n"
        )
        _make_executable(fake_work)

    run_env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    if env:
        run_env.update(env)

    result = subprocess.run(
        ["bash", str(CLAUDE_RUNNER)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    captured_argv = []
    if argv_file.exists():
        captured_argv = [line for line in argv_file.read_text().splitlines() if line]

    prompt = None
    if "--append-system-prompt-file" in captured_argv:
        idx = captured_argv.index("--append-system-prompt-file")
        prompt_path = Path(captured_argv[idx + 1])
        if prompt_path.exists():
            prompt = prompt_path.read_text()

    work_calls = []
    if work_calls_file.exists():
        work_calls = [line for line in work_calls_file.read_text().splitlines() if line]

    return ClaudeRunnerResult(
        output=result.stdout + result.stderr,
        argv=captured_argv,
        prompt=prompt,
        work_calls=work_calls,
    )
