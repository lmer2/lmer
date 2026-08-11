"""Host-side tests for bin/doctor's '🔗 Declared Sources' section.

Drives `bash bin/doctor` as a subprocess against a tmp work-repo root
(LMER_WORK_REPO_PATH) with the helper CLI stubbed via LMER_PYTHON — a shell
shim that returns canned JSON for `-m lmer_cli.container.sources` and
delegates every other invocation (the -c flatten/parse scripts) to the real
interpreter. git ls-remote is faked via a PATH-prepended `git`; the taskdef
clone dir uses the existing LMER_DOCTOR_TASKDEF_DIR seam. One case drives
the REAL helper end-to-end (LMER_PYTHON unset, PYTHONPATH=src/).

doctor runs many unrelated sections that may warn or error in a host
sandbox, so every assertion is scoped to the Declared Sources block (stdout
sliced between the section header and the next non-indented line) plus the
overall exit code — which must never be 2 (doctor itself broke).
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import strip_lmer_env

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "bin" / "doctor"
SRC_DIR = REPO_ROOT / "src"

SECTION_HEADER = "🔗 Declared Sources"
# warn() lines carry U+26A0; pass() lines carry the check mark.
WARN_MARK = "⚠"
PASS_MARK = "✓"

DECLARED_REPO = "https://git.example.com/group/taskdef.git"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so the subprocess env starts from a clean slate."""
    strip_lmer_env(monkeypatch)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _declared_sources_block(stdout):
    """Slice stdout to the Declared Sources section's own lines.

    Section content is indented (pass/warn/detail/skip notes); the block ends
    at the first non-indented, non-empty line (the next section header or the
    summary rule).
    """
    lines = stdout.splitlines()
    assert SECTION_HEADER in lines, (
        f"Declared Sources section missing from doctor output:\n{stdout}"
    )
    block = []
    for line in lines[lines.index(SECTION_HEADER) + 1:]:
        if line and not line.startswith(" "):
            break
        block.append(line)
    return "\n".join(block)


def _warn_lines(block):
    return [line for line in block.splitlines() if WARN_MARK in line]


def _helper_doc(clone_url, *, ref="main", repo=DECLARED_REPO, mode="https-userinfo",
                anonymous=False, warnings=(), errors=(), ok=True, schema=1):
    """Canned `sources doctor --json --emit-clone-urls` document (frozen seam)."""
    return {
        "ok": ok,
        "path": "unused-by-doctor",
        "present": True,
        "schema": schema,
        "work_repo_url": "https://git.example.com/group/work.git",
        "sources": {
            "taskdef": {
                "repo": repo,
                "ref": ref,
                "mode": mode,
                "anonymous": anonymous,
                "clone_url": clone_url,
                "clone_url_redacted": False,
            }
        },
        "warnings": list(warnings),
        "errors": list(errors),
        "supported_sources_schemas": [1],
        "supported_taskdef_schemas": [1, 2],
    }


def _write_stub_python(tmp_path, doc, no_emit_doc=None):
    """LMER_PYTHON stub: canned JSON for the helper module, real python otherwise.

    doctor also runs `$LMER_PYTHON -c '<flatten/parse script>'`, so everything
    that is not `-m lmer_cli.container.sources` execs the test interpreter.

    `no_emit_doc`, when given, is served instead of `doc` for an invocation
    WITHOUT `--emit-clone-urls` — i.e. the Declared-Sources loop gets `doc`
    and the taskdef check's legacy re-ask gets `no_emit_doc`, so a test can
    tell which of the two supplied a value.
    """
    canned = tmp_path / "canned-helper.json"
    canned.write_text(json.dumps(doc))
    fallback = tmp_path / "canned-helper-no-emit.json"
    fallback.write_text(json.dumps(no_emit_doc if no_emit_doc is not None else doc))
    stub = tmp_path / "stub-lmer-python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "lmer_cli.container.sources" ]; then\n'
        '    for arg in "$@"; do\n'
        '        if [ "$arg" = "--emit-clone-urls" ]; then\n'
        f"            cat {shlex.quote(str(canned))}\n"
        "            exit 0\n"
        "        fi\n"
        "    done\n"
        f"    cat {shlex.quote(str(fallback))}\n"
        "    exit 0\n"
        "fi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    stub.chmod(0o755)
    return stub


# git ls-remote behaviors for the PATH-prepended fake git (doctor invokes
# `[timeout 20] git ls-remote -- URL`, so $1 is always `ls-remote`).
GIT_OK_MAIN = (
    "    printf '1111111111111111111111111111111111111111\\trefs/heads/main\\n'\n"
    "    exit 0\n"
)
GIT_OK_OTHER_REFS = (
    "    printf '2222222222222222222222222222222222222222\\trefs/heads/other\\n'\n"
    "    printf '3333333333333333333333333333333333333333\\trefs/tags/v0\\n'\n"
    "    exit 0\n"
)
GIT_UNREACHABLE = "    exit 128\n"
# Enough refs to blow past the 64 KB pipe buffer, with the matched ref FIRST.
# This is the shape that makes an early-exiting matcher in a `set -o pipefail`
# pipeline report a present ref as missing (SIGPIPE → 141 → pipeline status).
GIT_OK_MANY_REFS = (
    "    printf '1111111111111111111111111111111111111111\\trefs/heads/main\\n'\n"
    "    seq 1 20000 | while read -r n; do\n"
    "        printf '%040d\\trefs/heads/branch-%s\\n' \"$n\" \"$n\"\n"
    "    done\n"
    "    exit 0\n"
)


def _git_log_path(tmp_path):
    """Where the fake git records each invocation's argv and git-config env."""
    return tmp_path / "fake-git-invocations.txt"


def _write_fake_git(tmp_path, ls_remote_body):
    git_dir = tmp_path / "fake-git-bin"
    git_dir.mkdir(exist_ok=True)
    log = _git_log_path(tmp_path)
    fake_git = git_dir / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        # Record argv and the GIT_CONFIG_* environment: tests assert both
        # where the credentialed URL travels and where it must not.
        f"LOG={shlex.quote(str(log))}\n"
        'for a in "$@"; do printf "ARGV %s\\n" "$a" >> "$LOG"; done\n'
        'env | grep "^GIT_CONFIG" | sed "s/^/ENV /" >> "$LOG" || true\n'
        'if [ "${1:-}" = "ls-remote" ]; then\n'
        f"{ls_remote_body}"
        "fi\n"
        "exit 0\n"
    )
    fake_git.chmod(0o755)
    return git_dir


def _git_invocations(tmp_path):
    """The fake git's recorded lines (`ARGV <word>` / `ENV GIT_CONFIG_…`)."""
    log = _git_log_path(tmp_path)
    return log.read_text().splitlines() if log.exists() else []


def _run_doctor(*, work, taskdef_dir, lmer_python=None, fake_git_dir=None,
                extra_env=None, verbose=False):
    """Run `bash bin/doctor` with a controlled environment; return the proc."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.pop("PYTHONPATH", None)
    env["LMER_WORK_REPO_PATH"] = str(work)
    env["LMER_DOCTOR_TASKDEF_DIR"] = str(taskdef_dir)
    if lmer_python is not None:
        env["LMER_PYTHON"] = str(lmer_python)
    if fake_git_dir is not None:
        env["PATH"] = os.pathsep.join([str(fake_git_dir), env.get("PATH", "")])
    if extra_env:
        env.update(extra_env)
    cmd = ["bash", str(DOCTOR)] + (["-v"] if verbose else [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

class TestDoctorDeclaredSources:
    """bin/doctor Declared Sources section, stubbed helper + fake git."""

    def test_no_sources_yaml_silent_legacy(self, tmp_path):
        """(a) Absent sources.yaml is a skip note, never a finding (spec G5)."""
        work = tmp_path / "work"
        work.mkdir()
        stub = _write_stub_python(tmp_path, _helper_doc(DECLARED_REPO))
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef", lmer_python=stub
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        assert f"no sources.yaml at {work}/sources.yaml" in block
        assert "silent legacy mode" in block
        assert _warn_lines(block) == [], block

    def test_all_sources_reachable(self, tmp_path):
        """(b) Valid config + reachable repo + present ref → pass lines only."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        stub = _write_stub_python(tmp_path, _helper_doc(DECLARED_REPO))
        git_dir = _write_fake_git(tmp_path, GIT_OK_MAIN)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir,
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        assert f"{PASS_MARK} sources.yaml valid (schema 1)" in block
        assert (
            f"taskdef source reachable: {DECLARED_REPO} (mode: https-userinfo)"
            in block
        )
        assert "taskdef ref 'main' present on remote" in block
        assert _warn_lines(block) == [], block

    def test_unreachable_source_warns_with_remediation(self, tmp_path):
        """(c) ls-remote failure → warn with remediation; exit stays 0/1."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        stub = _write_stub_python(tmp_path, _helper_doc(DECLARED_REPO))
        git_dir = _write_fake_git(tmp_path, GIT_UNREACHABLE)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir,
        )
        assert proc.returncode in (0, 1), proc.stderr
        block = _declared_sources_block(proc.stdout)
        warns = _warn_lines(block)
        assert len(warns) == 1, block
        assert "taskdef source unreachable" in warns[0]
        assert "git ls-remote failed" in warns[0]
        assert (
            "check host reachability and that the work-repo credential grants "
            "read access" in warns[0]
        )

    def test_withheld_credential_remediation_names_the_port(self, tmp_path):
        """The second anonymous mode reaches doctor as `anonymous: true` too,
        so the remediation must branch on the mode — telling an operator the
        work-repo URL "carries no credential" when it does, and the port is
        the actual reason, sends them to the wrong place.
        """
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        other_port = "https://git.example.com:5050/group/taskdefs.git"
        stub = _write_stub_python(
            tmp_path,
            _helper_doc(
                other_port, repo=other_port,
                mode="anonymous-other-port", anonymous=True,
            ),
        )
        git_dir = _write_fake_git(tmp_path, GIT_UNREACHABLE)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir,
        )
        assert proc.returncode in (0, 1), proc.stderr
        (warn,) = _warn_lines(_declared_sources_block(proc.stdout))
        assert "different port" in warn and "withheld" in warn
        assert "credential-less work-repo URL" not in warn

    def test_declared_ref_missing_warns(self, tmp_path):
        """(d) Repo reachable but the declared ref is not on the remote."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        stub = _write_stub_python(tmp_path, _helper_doc(DECLARED_REPO))
        git_dir = _write_fake_git(tmp_path, GIT_OK_OTHER_REFS)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir,
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        assert "taskdef source reachable" in block
        warns = _warn_lines(block)
        assert len(warns) == 1, block
        assert "taskdef ref 'main' not found on" in warns[0]
        assert "fix sources.taskdef.ref" in warns[0]

    def test_present_ref_survives_a_remote_with_many_refs(self, tmp_path):
        """(d2) A present ref must not be reported missing on a big remote.

        Regression: the ref check used `printf | cut | grep -Fqx`. `grep -q`
        exits at the first match, `cut` then dies of SIGPIPE (141) with output
        still buffered, and `set -o pipefail` promotes 141 to the pipeline's
        status — so the match evaluated as false whenever the output exceeded
        the pipe buffer and the ref was found early. Small fixtures (every
        other case here) never tripped it.
        """
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        stub = _write_stub_python(tmp_path, _helper_doc(DECLARED_REPO))
        git_dir = _write_fake_git(tmp_path, GIT_OK_MANY_REFS)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir,
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        assert "taskdef ref 'main' present on remote" in block
        assert _warn_lines(block) == [], block

    def test_taskdef_dir_absent_is_skip_note(self, tmp_path):
        """(e) No taskdef clone → explicit skip note, zero warnings."""
        work = tmp_path / "work"
        work.mkdir()
        absent = tmp_path / "no-taskdef"
        stub = _write_stub_python(tmp_path, _helper_doc(DECLARED_REPO))
        proc = _run_doctor(work=work, taskdef_dir=absent, lmer_python=stub)
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        assert f"no taskdef clone at {absent} — taskdef schema check skipped" in block
        assert _warn_lines(block) == [], block

    def test_unsupported_taskdef_schema_warns(self, tmp_path):
        """(f) taskdef.yaml declaring an unsupported schema → warn.

        No sources.yaml, so this also exercises the legacy re-ask path: the
        supported set comes from a second helper (stub) invocation.
        """
        work = tmp_path / "work"
        work.mkdir()
        taskdef_dir = tmp_path / "taskdef"
        taskdef_dir.mkdir()
        (taskdef_dir / "taskdef.yaml").write_text("schema: 99\n")
        stub = _write_stub_python(tmp_path, _helper_doc(DECLARED_REPO))
        proc = _run_doctor(work=work, taskdef_dir=taskdef_dir, lmer_python=stub)
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        warns = _warn_lines(block)
        assert len(warns) == 1, block
        assert "taskdef declares schema 99" in warns[0]
        assert "supports only: 1,2" in warns[0]

    def test_helper_unavailable_single_warning(self, tmp_path):
        """(g) Helper exits non-zero with no output → exactly one warn, no crash."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python="/bin/false",
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        warns = _warn_lines(block)
        assert len(warns) == 1, block
        assert "sources helper is unavailable" in warns[0]
        assert "declared sources unchecked" in warns[0]

    def test_credentialed_clone_url_never_printed(self, tmp_path):
        """(h) The unredacted clone_url never reaches output.

        Runs verbose with an unreachable repo (the chattiest path) and asserts
        the token/userinfo from the helper's clone_url never reaches output.
        """
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        credentialed = "https://x-token:sekrit123@git.example.com/group/taskdef.git"
        stub = _write_stub_python(tmp_path, _helper_doc(credentialed))
        git_dir = _write_fake_git(tmp_path, GIT_UNREACHABLE)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir, verbose=True,
        )
        assert proc.returncode != 2, proc.stderr
        combined = proc.stdout + proc.stderr
        assert "sekrit123" not in combined
        assert "x-token:" not in combined
        # The warn path still fired, printing only the scrubbed repo URL.
        block = _declared_sources_block(proc.stdout)
        assert "taskdef source unreachable" in block
        assert DECLARED_REPO in block

    def test_credentialed_clone_url_never_reaches_git_argv(self, tmp_path):
        """(h2) git gets the token through env config, never through argv.

        `/proc/<pid>/cmdline` is readable by every other process in the
        container, so the credentialed URL is handed to git as an ephemeral
        `url.<credentialed>.insteadOf` rewrite; argv carries only the scrubbed
        declared URL, and the rewrite is what still makes the fetch work.
        """
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        credentialed = "https://x-token:sekrit123@git.example.com/group/taskdef.git"
        stub = _write_stub_python(tmp_path, _helper_doc(credentialed))
        git_dir = _write_fake_git(tmp_path, GIT_OK_MAIN)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir, verbose=True,
        )
        assert proc.returncode != 2, proc.stderr
        recorded = _git_invocations(tmp_path)
        argv = [line for line in recorded if line.startswith("ARGV ")]
        assert argv, recorded
        assert not any("sekrit123" in line for line in argv), argv
        assert f"ARGV {DECLARED_REPO}" in argv
        # The credential travelled in the environment instead …
        rewrites = [
            line for line in recorded
            if line.startswith("ENV GIT_CONFIG_KEY_") and ".insteadOf" in line
        ]
        assert rewrites and all("sekrit123" in line for line in rewrites), recorded
        assert f"ENV GIT_CONFIG_VALUE_0={DECLARED_REPO}" in recorded
        assert "ENV GIT_CONFIG_COUNT=1" in recorded
        # … and the check still resolved through it.
        block = _declared_sources_block(proc.stdout)
        assert "taskdef source reachable" in block
        assert "taskdef ref 'main' present on remote" in block

    def test_inherited_git_config_entries_are_not_clobbered(self, tmp_path):
        """(h3) The ephemeral rewrite appends after inherited numbered config."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        credentialed = "https://x-token:sekrit123@git.example.com/group/taskdef.git"
        stub = _write_stub_python(tmp_path, _helper_doc(credentialed))
        git_dir = _write_fake_git(tmp_path, GIT_OK_MAIN)
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            lmer_python=stub, fake_git_dir=git_dir,
            extra_env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.sslVerify",
                "GIT_CONFIG_VALUE_0": "true",
            },
        )
        assert proc.returncode != 2, proc.stderr
        recorded = _git_invocations(tmp_path)
        assert "ENV GIT_CONFIG_COUNT=2" in recorded
        assert "ENV GIT_CONFIG_KEY_0=http.sslVerify" in recorded
        assert "ENV GIT_CONFIG_VALUE_0=true" in recorded
        assert any(
            line.startswith("ENV GIT_CONFIG_KEY_1=url.") and ".insteadOf" in line
            for line in recorded
        ), recorded

    def test_env_rewrite_is_what_real_git_follows(self, tmp_path):
        """(h5) REAL git (no fake on PATH) reaches the repo via the env rewrite.

        The declared URL in argv is unresolvable on purpose; only the
        ephemeral `url.<clone_url>.insteadOf` entry can make `git ls-remote`
        succeed. If the mechanism ever stops working, this fails with the
        unreachable warning instead of passing on a fake git's `exit 0`.
        """
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        upstream = tmp_path / "upstream.git"
        seed = tmp_path / "seed"
        seed.mkdir()
        for cmd in (
            ["git", "init", "-q", "--bare", str(upstream)],
            ["git", "init", "-q", "-b", "main", str(seed)],
            ["git", "-C", str(seed), "-c", "user.email=t@example.com",
             "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "seed"],
            ["git", "-C", str(seed), "push", "-q", str(upstream), "main"],
        ):
            subprocess.run(cmd, check=True, capture_output=True)
        stub = _write_stub_python(tmp_path, _helper_doc(str(upstream)))
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef", lmer_python=stub,
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        assert f"taskdef source reachable: {DECLARED_REPO}" in block
        assert "taskdef ref 'main' present on remote" in block
        assert _warn_lines(block) == [], block

    def test_supported_schemas_survive_the_record_loop(self, tmp_path):
        """(h4) The record loop must run in the CURRENT shell.

        `SUPPORTED_TASKDEF_SCHEMAS_CSV` is set from the loop's META record and
        read after the loop by the taskdef schema check. Process substitution
        keeps the loop body in the current shell (as the here-string it
        replaced did); a plain pipe would drop the value and silently fall
        back to the legacy re-ask. The stub answers the re-ask with a
        different supported set, so the two sources are distinguishable.
        """
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text("schema: 1\n")
        taskdef_dir = tmp_path / "taskdef"
        taskdef_dir.mkdir()
        (taskdef_dir / "taskdef.yaml").write_text("schema: 99\n")
        doc = _helper_doc(DECLARED_REPO)
        doc["supported_taskdef_schemas"] = [1, 2, 3]
        re_ask = _helper_doc(DECLARED_REPO)
        re_ask["supported_taskdef_schemas"] = [7]
        stub = _write_stub_python(tmp_path, doc, no_emit_doc=re_ask)
        git_dir = _write_fake_git(tmp_path, GIT_OK_MAIN)
        proc = _run_doctor(
            work=work, taskdef_dir=taskdef_dir,
            lmer_python=stub, fake_git_dir=git_dir,
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        warns = _warn_lines(block)
        assert len(warns) == 1, block
        assert "supports only: 1,2,3" in warns[0]

    def test_real_helper_end_to_end(self, tmp_path):
        """(i) Real helper: LMER_PYTHON unset, PYTHONPATH=src/, real sources.yaml.

        `python3` resolves to the test interpreter's bin dir (PATH prepend);
        the branch's lmer_cli.container.sources parses the file, derives the
        credentialed clone URL from LMER_REPO_URL, and the fake git answers
        ls-remote. The work-repo token must never appear in output.
        """
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text(
            "schema: 1\n"
            "sources:\n"
            "  taskdef:\n"
            f"    repo: {DECLARED_REPO}\n"
            "    ref: main\n"
        )
        git_dir = _write_fake_git(tmp_path, GIT_OK_MAIN)
        python_bin_dir = Path(sys.executable).parent
        proc = _run_doctor(
            work=work, taskdef_dir=tmp_path / "no-taskdef",
            fake_git_dir=git_dir,
            extra_env={
                "PYTHONPATH": str(SRC_DIR),
                "PATH": os.pathsep.join(
                    [str(git_dir), str(python_bin_dir), os.environ.get("PATH", "")]
                ),
                "LMER_REPO_URL": (
                    "https://oauth2:hosttoken456@git.example.com/group/work.git"
                ),
            },
        )
        assert proc.returncode != 2, proc.stderr
        block = _declared_sources_block(proc.stdout)
        assert f"{PASS_MARK} sources.yaml valid (schema 1)" in block
        assert (
            f"taskdef source reachable: {DECLARED_REPO} (mode: https-userinfo)"
            in block
        )
        assert "taskdef ref 'main' present on remote" in block
        assert _warn_lines(block) == [], block
        assert "hosttoken456" not in proc.stdout + proc.stderr
