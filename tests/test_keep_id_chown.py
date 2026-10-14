"""Issues #351 and #384 — podman's one-time whole-rootfs UID remap.

Three behaviours:

* the remap is **pre-warmed** after a build or pull, so it is paid where a
  human is already waiting rather than at the first spawn (#351, the piece
  MR !249 deferred);
* the launch notice ("may take several minutes … remaps UIDs") is gated on
  whether THE IMAGE has been remapped, not on whether this call created it.
  Gating it on ``materialized`` left ``lmer build`` + a separate spawn — the
  longest wait there is — completely silent (#384);
* ``libexec/verify-keep-id-chown.sh`` must be usable before the first build
  (``--skip-timing``) and must find lmer's own interpreter rather than
  assuming the system ``python3`` (a ``uv tool install`` keeps lmer in a
  private venv).
"""

import os
import stat
import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lmer_cli import keep_id, runtime as runtime_mod
from lmer_cli.build import ImageReadiness, build_image, ensure_image


REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "libexec" / "verify-keep-id-chown.sh"

NOTICE = "remaps UIDs for --userns=keep-id"


# ---------------------------------------------------------------------------
# The launch notice
# ---------------------------------------------------------------------------


IMAGE_ID = "sha256:" + "a" * 64
OTHER_IMAGE_ID = "sha256:" + "b" * 64


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point ~/.lmer at a throwaway directory: these tests write remap markers."""
    monkeypatch.setattr(runtime_mod, "_LMER_STATE_DIR", tmp_path / "lmer-state")
    return tmp_path / "lmer-state"


def _run_main(argv_extra, readiness, runtime="podman", euid=1000,
              engine_rootless=True, status=0, captured=None):
    """Run main() through the shared container-less mock stack and return
    everything it printed.

    *engine_rootless* is what ``podman info`` would answer about the engine
    the client is talking to — True/False, or None for "could not tell". It
    is a separate knob from *euid* on purpose: a non-root client can be
    pointed at a rootful engine, which is finding F1 of the !309 adversarial
    review.
    """
    from tests.test_lmer_cli_slack_target import (
        _BASE_ENV,
        _make_main_mocks,
        REPO_URL,
    )

    argv = ["chat", "--no-task", "--exec", "true", *argv_extra, REPO_URL]
    with patch.dict(os.environ, _BASE_ENV, clear=True):
        with _make_main_mocks(captured):
            with patch("lmer_cli.cli.detect_runtime", return_value=runtime), \
                 patch("lmer_cli.cli.os.geteuid", return_value=euid), \
                 patch("lmer_cli.cli.engine_is_rootless", return_value=engine_rootless), \
                 patch("lmer_cli.cli.subprocess.call", return_value=status), \
                 patch("lmer_cli.cli.ensure_image", MagicMock(return_value=readiness)):
                from lmer_cli.cli import main

                return main(argv)


class TestRemapNotice:
    """The notice is gated on the image's recorded remap state (#384), which
    is the only signal that survives the build and the spawn being separate
    processes."""

    def test_cold_spawn_after_a_separate_build_warns(self, state_dir, capsys):
        """#384's defect: `lmer build` then a spawn is materialized=False AND
        cold, and used to print nothing for ~12 minutes."""
        rc = _run_main(
            [], ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID)
        )
        out = capsys.readouterr()
        assert rc == 0
        assert NOTICE in out.out + out.err, (
            "a spawn of an image nothing has remapped yet must say so — a "
            "silent terminal for the whole chown reads as a hang (#384)"
        )

    def test_recorded_remap_is_silent(self, state_dir, capsys):
        keep_id.record_remap(IMAGE_ID)
        rc = _run_main(
            [], ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID)
        )
        out = capsys.readouterr()
        assert rc == 0
        assert NOTICE not in out.out + out.err, (
            "the multi-minute remap notice fired on a launch of an image that "
            "has already been remapped — the #351 defect the host test found"
        )

    def test_rebuild_under_the_same_tag_warns_again(self, state_dir, capsys):
        """The marker is keyed on the image ID, so a rebuild — same tag, new
        content — is correctly cold again."""
        keep_id.record_remap(OTHER_IMAGE_ID)
        rc = _run_main(
            [], ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID)
        )
        out = capsys.readouterr()
        assert rc == 0
        assert NOTICE in out.out + out.err

    def test_freshly_built_image_warns(self, state_dir, capsys):
        rc = _run_main(
            [], ImageReadiness(available=True, materialized=True, image_id=IMAGE_ID)
        )
        out = capsys.readouterr()
        assert rc == 0
        assert NOTICE in out.out + out.err

    def test_root_userns_never_warns(self, state_dir, capsys):
        """--root-userns skips keep-id entirely, so there is no remap to warn
        about however cold the image is."""
        rc = _run_main(
            ["--root-userns"],
            ImageReadiness(available=True, materialized=True, image_id=IMAGE_ID),
        )
        out = capsys.readouterr()
        assert rc == 0
        assert NOTICE not in out.out + out.err

    def test_docker_start_is_silent_even_when_freshly_built(self, state_dir, capsys):
        """keep-id is a rootless-podman mechanism; docker never remaps."""
        rc = _run_main(
            [],
            ImageReadiness(available=True, materialized=True, image_id=IMAGE_ID),
            runtime="docker",
        )
        out = capsys.readouterr()
        assert rc == 0
        assert NOTICE not in out.out + out.err

    def test_a_completed_keep_id_launch_records_the_remap(self, state_dir, capsys):
        """Otherwise the launch that paid the chown itself would warn again on
        every subsequent start of the same image."""
        _run_main(
            [], ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID)
        )
        assert keep_id.remap_recorded(IMAGE_ID) is True

    def test_docker_launch_records_nothing(self, state_dir, capsys):
        _run_main(
            [],
            ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID),
            runtime="docker",
        )
        assert keep_id.remap_recorded(IMAGE_ID) is False


class TestRemapMarker:
    """Where the marker lives, which is what makes it work across processes."""

    def test_marker_is_in_the_shared_state_dir_not_the_cwd(self, state_dir, tmp_path,
                                                           monkeypatch):
        """A per-invocation or per-cwd location would leave the platform
        daemon's spawns exactly as blind as #384 found them."""
        monkeypatch.chdir(tmp_path)
        first = keep_id.remap_marker_path(IMAGE_ID)
        monkeypatch.chdir(tmp_path.parent)
        assert keep_id.remap_marker_path(IMAGE_ID) == first
        assert first.parent == state_dir / "remapped"

    def test_platform_spawn_hands_the_child_its_own_environment(
            self, monkeypatch):
        """The daemon spawns `lmer` as a subprocess, so the child reads the
        marker under the daemon's HOME — which holds only while the child env
        is built from os.environ rather than a curated subset."""
        from lmer_cli.env_sources import env_with_overrides

        source = (Path(__file__).parent.parent
                  / "src" / "lmer_platform" / "spawn.py").read_text()
        assert "child_env = env_with_overrides(" in source, (
            "lmer_platform.spawn no longer builds the child env through "
            "env_with_overrides; check that HOME still reaches the spawned "
            "lmer, or the keep-id remap marker becomes invisible to platform "
            "spawns (#384)"
        )
        monkeypatch.setenv("HOME", "/daemon/home")
        assert env_with_overrides({"X": "1"})["HOME"] == "/daemon/home", (
            "env_with_overrides no longer inherits the daemon's environment "
            "wholesale, so the spawned lmer loses the daemon's HOME and the "
            "keep-id remap marker becomes invisible to platform spawns (#384)"
        )


class TestPrewarm:
    """The remap is paid at build time, not at first spawn (#351)."""

    def _call(self, monkeypatch, rc=0, runtime="podman", **kwargs):
        calls = []

        def fake_call(cmd, **kw):
            calls.append((cmd, kw))
            return rc

        monkeypatch.setattr(keep_id.subprocess, "call", fake_call)
        monkeypatch.setattr(keep_id, "image_id", lambda *a, **k: IMAGE_ID)
        warmed = keep_id.prewarm(runtime, "lmer:dev-abc", **kwargs)
        return warmed, calls

    def test_prewarm_runs_a_keep_id_container(self, state_dir, monkeypatch):
        warmed, calls = self._call(monkeypatch)
        assert warmed is True
        assert len(calls) == 1
        cmd = calls[0][0]
        assert cmd[:3] == ["podman", "run", "--rm"]
        assert "--userns=keep-id" in cmd
        # The resolved ID, not the tag: see test_prewarm_runs_the_id_it_records.
        assert cmd[-1] == IMAGE_ID

    def test_prewarm_does_not_run_the_image_entrypoint(self, state_dir, monkeypatch):
        """The entrypoint installs mise tools over the network and rewrites the
        container home; the remap needs none of it."""
        _, calls = self._call(monkeypatch)
        cmd = calls[0][0]
        assert "--entrypoint" in cmd
        assert cmd[cmd.index("--entrypoint") + 1] == "/usr/bin/true"

    def test_prewarm_passes_no_resource_flags(self, state_dir, monkeypatch):
        """--cpus/--memory/--pids-limit are what makes crun abort when the
        cgroup controllers are not delegated to the user slice
        (runtime._resource_limit_args). A throwaway `true` needs no bound."""
        _, calls = self._call(monkeypatch)
        cmd = calls[0][0]
        for flag in ("--cpus", "--memory", "--pids-limit"):
            assert flag not in cmd

    def test_prewarm_is_bounded(self, state_dir, monkeypatch):
        _, calls = self._call(monkeypatch)
        assert calls[0][1]["timeout"] == keep_id.DEFAULT_PREWARM_TIMEOUT

    def test_prewarm_records_the_remap(self, state_dir, monkeypatch):
        self._call(monkeypatch)
        assert keep_id.remap_recorded(IMAGE_ID) is True

    def test_a_failed_prewarm_records_nothing(self, state_dir, monkeypatch):
        warmed, _ = self._call(monkeypatch, rc=125)
        assert warmed is False
        assert keep_id.remap_recorded(IMAGE_ID) is False

    def test_prewarm_rewrites_an_existing_marker(self, state_dir, monkeypatch):
        """Unconditional, so a stale marker left by a `podman system reset`
        self-heals on the next build instead of needing a detector."""
        keep_id.record_remap(IMAGE_ID)
        _, calls = self._call(monkeypatch)
        assert len(calls) == 1

    def test_prewarm_skipped_for_docker(self, state_dir, monkeypatch):
        warmed, calls = self._call(monkeypatch, runtime="docker")
        assert (warmed, calls) == (False, [])

    def test_prewarm_skipped_for_container_root(self, state_dir, monkeypatch):
        warmed, calls = self._call(monkeypatch, container_root=True)
        assert (warmed, calls) == (False, [])

    def test_prewarm_opt_out(self, state_dir, monkeypatch):
        monkeypatch.setenv("LMER_NO_PREWARM", "1")
        warmed, calls = self._call(monkeypatch)
        assert (warmed, calls) == (False, [])

    def test_prewarm_timeout_override(self, state_dir, monkeypatch):
        monkeypatch.setenv("LMER_PREWARM_TIMEOUT", "90")
        _, calls = self._call(monkeypatch)
        assert calls[0][1]["timeout"] == 90

    def test_prewarm_timeout_rejects_nonsense(self, state_dir, monkeypatch):
        monkeypatch.setenv("LMER_PREWARM_TIMEOUT", "soon")
        _, calls = self._call(monkeypatch)
        assert calls[0][1]["timeout"] == keep_id.DEFAULT_PREWARM_TIMEOUT


class TestPrewarmCallSites:
    """Both entry points warm the image: `lmer build`, and the auto-build/pull
    the launch path takes — which is the only podman call site the platform
    daemon reaches, since it spawns `lmer` as a subprocess."""

    def test_lmer_build_prewarms(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.build_image_local", return_value=0), \
             patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.prewarm") as mock_prewarm:
            assert build_image("podman", "lmer:dev-abc", tmp_path) is True
        mock_prewarm.assert_called_once_with(
            "podman", "lmer:dev-abc", container_root=False
        )

    def test_a_failed_build_does_not_prewarm(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.build_image_local", return_value=1), \
             patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.prewarm") as mock_prewarm:
            assert build_image("podman", "lmer:dev-abc", tmp_path) is False
        mock_prewarm.assert_not_called()

    def test_auto_build_prewarms(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=0), \
             patch("lmer_cli.build.resolve_image_id", return_value=IMAGE_ID), \
             patch("lmer_cli.build.prewarm") as mock_prewarm:
            result = ensure_image("podman", "lmer:dev-abc", tmp_path)
        assert result.image_id == IMAGE_ID
        mock_prewarm.assert_called_once_with(
            "podman", "lmer:dev-abc", container_root=False
        )

    def test_registry_pull_prewarms(self):
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.pull_image", return_value=0), \
             patch("lmer_cli.build.resolve_image_id", return_value=IMAGE_ID), \
             patch("lmer_cli.build.prewarm") as mock_prewarm:
            result = ensure_image("podman", "lmer:dev-abc", None)
        assert result.image_id == IMAGE_ID
        mock_prewarm.assert_called_once()

    def test_root_userns_launch_passes_container_root_through(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=0), \
             patch("lmer_cli.build.resolve_image_id", return_value=IMAGE_ID), \
             patch("lmer_cli.build.prewarm") as mock_prewarm:
            ensure_image("podman", "lmer:dev-abc", tmp_path, container_root=True)
        mock_prewarm.assert_called_once_with(
            "podman", "lmer:dev-abc", container_root=True
        )

    def test_lmer_build_honours_the_root_userns_env_toggle(self, tmp_path, monkeypatch):
        """`lmer build` has no --root-userns flag, so the env toggle is the
        only signal it has; without it a host on the fast path would pay the
        7-12 minute remap inside every rebuild and then never use it."""
        monkeypatch.setenv("LMER_ROOT_USERNS", "1")
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.build_image_local", return_value=0), \
             patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.prewarm") as mock_prewarm:
            build_image("podman", "lmer:dev-abc", tmp_path)
        mock_prewarm.assert_called_once_with(
            "podman", "lmer:dev-abc", container_root=True
        )

    def test_lmer_build_honours_a_root_container_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_CONTAINER_USER", "root")
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.build_image_local", return_value=0), \
             patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.prewarm") as mock_prewarm:
            build_image("podman", "lmer:dev-abc", tmp_path)
        mock_prewarm.assert_called_once_with(
            "podman", "lmer:dev-abc", container_root=True
        )


class TestKeepIdCondition:
    """base_run_args and the notice must not drift apart about when keep-id
    applies."""

    def test_ambient_user_matches_what_a_flagless_launch_resolves(self, monkeypatch):
        """`lmer build` reads the ambient user; a launch layers flags on top of
        the same function, so the two cannot disagree about a flagless run."""
        from types import SimpleNamespace

        from lmer_cli.cli import _resolve_container_user

        flagless = SimpleNamespace(match_uid=False, user=None, root_userns=False)
        for env in ({}, {"LMER_ROOT_USERNS": "1"}, {"LMER_CONTAINER_USER": "root"}):
            monkeypatch.delenv("LMER_ROOT_USERNS", raising=False)
            monkeypatch.delenv("LMER_CONTAINER_USER", raising=False)
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            assert _resolve_container_user(flagless)[0] == \
                runtime_mod.ambient_container_user()

    def test_flag_and_notice_share_one_predicate(self):
        for rt, user in (("podman", "developer"), ("podman", "0:0"),
                         ("docker", "developer"), ("podman", "root")):
            expected = "--userns=keep-id" in runtime_mod.base_run_args(rt, True, user)
            assert runtime_mod.keep_id_applies(rt, user) is expected


# ---------------------------------------------------------------------------
# libexec/verify-keep-id-chown.sh
# ---------------------------------------------------------------------------


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _stub_bin(tmp_path, *, lmer_python=None, python3_imports=False):
    """A PATH directory holding a podman stub (and optionally an ``lmer``
    entry point plus a ``python3``) so the script can run with no container
    runtime and no lmer install.

    The podman stub appends every invocation to ``calls.log`` and reports the
    image as absent, which is the state the script must cope with before the
    first build.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    _write_exec(bin_dir / "podman", f'''#!/usr/bin/env bash
echo "$@" >> "{calls}"
case "$1 ${{2:-}}" in
    "info ") exit 0 ;;
    "image exists") exit 1 ;;
esac
exit 0
''')
    # A python3 that cannot import lmer_cli is the operator's host: lmer is a
    # `uv tool install`, so the system interpreter knows nothing about it.
    marker = "lmer:dev-deadbeef1234"
    _write_exec(bin_dir / "python3", f'''#!/usr/bin/env bash
case "${{2:-}}" in
    *resolve_image_tag*) {'echo "${LMER_IMAGE:-' + marker + '}"' if python3_imports else 'exit 1'} ;;
    *detect_runtime*) {'echo podman' if python3_imports else 'exit 1'} ;;
    *repo_root_path*) {'echo ""' if python3_imports else 'exit 1'} ;;
    *"import lmer_cli"*) exit {0 if python3_imports else 1} ;;
esac
exit 1
''')
    if lmer_python is not None:
        _write_exec(bin_dir / "lmer", f"#!{lmer_python}\n# lmer entry point stub\n")
    return bin_dir, calls, marker


def _lmer_venv_python(tmp_path):
    """An interpreter that behaves like the one lmer is installed into: it
    can import lmer_cli and answers the script's two probes."""
    return _write_exec(tmp_path / "venv-python", '''#!/usr/bin/env bash
case "${2:-}" in
    # LMER_IMAGE first, mirroring _handle_build: the script must resolve the
    # image the build will really target, not resolve_image_tag alone.
    *resolve_image_tag*) echo "${LMER_IMAGE:-lmer:dev-deadbeef1234}" ;;
    *detect_runtime*)
        if [ -z "${STUB_RUNTIME+set}" ]; then echo podman
        elif [ -n "$STUB_RUNTIME" ]; then echo "$STUB_RUNTIME"
        fi ;;
    *repo_root_path*) echo "" ;;
    *"import lmer_cli"*) exit 0 ;;
    *) exit 1 ;;
esac
exit 0
''')


def _run_script(bin_dir, args, extra_env=None):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(bin_dir.parent),
             **(extra_env or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestVerifyScriptSkipTiming:
    """--skip-timing is the only verification available before the first
    build, so it must not be gated behind an image-presence check."""

    def test_skip_timing_succeeds_with_no_image_built(self, tmp_path):
        bin_dir, calls, _ = _stub_bin(tmp_path, python3_imports=True)
        result = _run_script(bin_dir, ["--skip-timing"])
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "(timings skipped)" in result.stdout
        logged = calls.read_text() if calls.exists() else ""
        assert "image exists" not in logged, (
            "--skip-timing still reached the image-presence check"
        )
        assert "run " not in logged, "--skip-timing still started a container"

    def test_without_skip_timing_an_absent_image_is_still_an_error(self, tmp_path):
        """Control: the image checks are intact, --skip-timing is what moves."""
        bin_dir, _, _ = _stub_bin(tmp_path)
        result = _run_script(bin_dir, ["--image", "lmer:absent"])
        assert result.returncode == 2
        assert "not present locally" in result.stderr


class TestVerifyScriptMeasureBuild:
    """--measure-build is destructive (`lmer build --force`) and is the
    evidence path the MR's timing row defers to, so it must settle WHICH image
    it measures before it rebuilds anything (!309 review)."""

    def _bin(self, tmp_path):
        bin_dir, calls, marker = _stub_bin(tmp_path)
        build_log = tmp_path / "lmer-build.log"
        _write_exec(bin_dir / "lmer", f'''#!/usr/bin/env bash
echo "$@" >> "{build_log}"
exit 0
''')
        return bin_dir, calls, build_log, marker

    def test_a_mismatched_image_is_refused_before_the_rebuild(self, tmp_path):
        bin_dir, _, build_log, _ = self._bin(tmp_path)
        venv_python = _lmer_venv_python(tmp_path)
        result = _run_script(
            bin_dir,
            ["--measure-build", "--python", str(venv_python), "--image", "lmer:other"],
        )
        assert result.returncode == 2
        assert "lmer build" in result.stderr
        assert not build_log.exists(), (
            "the destructive rebuild ran for an image the timing would not be "
            "about"
        )

    def test_an_unresolvable_tag_is_refused_before_the_rebuild(self, tmp_path):
        """No --image and an interpreter that cannot import lmer_cli: the run
        used to rebuild and only then discover it had nothing to time."""
        bin_dir, _, build_log, _ = self._bin(tmp_path)
        result = _run_script(bin_dir, ["--measure-build"])
        assert result.returncode == 2
        assert not build_log.exists()

    def test_the_matching_case_builds_then_times_one_keep_id_start(self, tmp_path):
        bin_dir, calls, build_log, marker = self._bin(tmp_path)
        venv_python = _lmer_venv_python(tmp_path)
        result = _run_script(
            bin_dir,
            ["--measure-build", "--python", str(venv_python), "--image", marker],
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "build --force" in build_log.read_text()
        started = [
            line for line in calls.read_text().splitlines()
            if line.startswith("run ")
        ]
        assert len(started) == 1, started
        assert "--userns=keep-id" in started[0]
        assert marker in started[0]

    def test_marker_state_agrees_with_the_python_side(self, tmp_path, monkeypatch):
        """`marker: absent -> present` is the review's own acceptance criterion
        for the pending timing row, so the script's `$HOME/.lmer/remapped/...`
        and `keep_id.remap_marker_path` have to name the same file. They are
        written in different languages and nothing else compares them."""
        image_id = "sha256:cafe1234567890abcdef"
        bin_dir, _, _, marker = self._bin(tmp_path)
        _write_exec(bin_dir / "podman", f'''#!/usr/bin/env bash
case "$1 ${{2:-}}" in
    "info ") exit 0 ;;
    "image exists") exit 1 ;;
    "image inspect") echo "{image_id}"; exit 0 ;;
esac
exit 0
''')
        venv_python = _lmer_venv_python(tmp_path)
        args = ["--measure-build", "--python", str(venv_python)]

        before = _run_script(bin_dir, args)
        assert "marker: absent" in before.stdout, before.stdout

        # _run_script runs the script with HOME=bin_dir.parent.
        monkeypatch.setattr(runtime_mod, "_LMER_STATE_DIR", bin_dir.parent / ".lmer")
        keep_id.record_remap(image_id)

        after = _run_script(bin_dir, args)
        assert "marker: present" in after.stdout, (
            "the script reports a marker the pre-warm just wrote as absent — "
            f"shell and Python disagree on the path\n{after.stdout}"
        )

    def test_the_rebuild_happens_before_the_timed_start(self, tmp_path):
        """Ordering is the whole point: a start timed before the rebuild would
        be timing the old, already-warm image."""
        bin_dir, calls, build_log, marker = self._bin(tmp_path)
        venv_python = _lmer_venv_python(tmp_path)
        _run_script(
            bin_dir, ["--measure-build", "--python", str(venv_python)]
        )
        assert build_log.stat().st_mtime <= calls.stat().st_mtime


class TestVerifyScriptStabilityProbe:
    """The tag-stability probe commits a real source change, which means it
    meets whatever the checkout's git hooks do."""

    def _repo(self, tmp_path, hook_marker):
        """A scratch checkout with a tracked src/ file and a pre-commit hook
        that fails — the state of any developer who followed the project's own
        "install the hooks" instruction."""
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "thing.py").write_text("x = 1\n")
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@localhost"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
        subprocess.run([*git, "add", "src/thing.py"], check=True, timeout=30)
        subprocess.run([*git, "commit", "-qm", "init"], check=True, timeout=30)
        _write_exec(
            repo / ".git" / "hooks" / "pre-commit",
            f'#!/usr/bin/env bash\ntouch "{hook_marker}"\nexit 1\n',
        )
        return repo

    def test_probe_commit_does_not_run_the_checkouts_hooks(self, tmp_path):
        hook_marker = tmp_path / "hook-ran"
        repo = self._repo(tmp_path, hook_marker)
        venv_python = _write_exec(
            tmp_path / "venv-python",
            "#!/usr/bin/env bash\n"
            'case "${2:-}" in\n'
            '    *resolve_image_tag*) echo "lmer:dev-deadbeef1234" ;;\n'
            f'    *repo_root_path*) echo "{repo}" ;;\n'
            '    *"import lmer_cli"*) exit 0 ;;\n'
            "    *) exit 1 ;;\n"
            "esac\n"
            "exit 0\n",
        )
        bin_dir, _, _ = _stub_bin(tmp_path, lmer_python=venv_python)
        result = _run_script(bin_dir, ["--skip-timing"])
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "OK: tag unchanged across a source-only commit to src/" in result.stdout
        assert not hook_marker.exists(), (
            "the throwaway worktree's commit ran the checkout's pre-commit "
            "hooks — a hook failure would abort the script under `set -e` and "
            "the operator would see a hook error where the tag verdict belongs"
        )


class TestVerifyScriptInterpreter:
    """The tag checks need the interpreter lmer is installed into."""

    def test_interpreter_comes_from_the_lmer_shebang(self, tmp_path):
        venv_python = _lmer_venv_python(tmp_path)
        bin_dir, _, marker = _stub_bin(
            tmp_path, lmer_python=venv_python, python3_imports=False
        )
        result = _run_script(bin_dir, ["--skip-timing"])
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert str(venv_python) in result.stdout, (
            "the script did not resolve lmer's own interpreter from its "
            "entry-point shebang and fell back to a python3 that cannot "
            "import lmer_cli"
        )
        assert f"resolved tag: {marker}" in result.stdout

    def test_python_override_wins(self, tmp_path):
        venv_python = _lmer_venv_python(tmp_path)
        bin_dir, _, marker = _stub_bin(tmp_path, python3_imports=False)
        result = _run_script(
            bin_dir, ["--skip-timing", "--python", str(venv_python)]
        )
        assert result.returncode == 0
        assert f"resolved tag: {marker}" in result.stdout

    def test_unusable_interpreter_hint_names_the_interpreter_and_the_flag(
        self, tmp_path
    ):
        """The old hint ("is lmer importable on this host?") sent the operator
        after a working install; the message must name what actually failed."""
        bin_dir, _, _ = _stub_bin(tmp_path, python3_imports=False)
        result = _run_script(bin_dir, ["--skip-timing"])
        assert "cannot import lmer_cli with" in result.stderr
        assert "--python" in result.stderr
        assert "is lmer importable on this host?" not in result.stderr
        assert result.returncode == 2, (
            "--skip-timing runs nothing but the tag checks, so a run that "
            "could not do them must not report success"
        )
        assert "nothing was verified" in result.stderr


class TestAdversarialReviewFindings:
    """Regression cover for the !309 adversarial (astra) second opinion.

    Each test names its finding and fails on the pre-fix tree. They live
    together because they share one origin, not one mechanism.
    """

    # -- F1: the rootless gate must establish the ENGINE is rootless ---------

    def test_a_rootful_engine_is_refused_even_from_a_non_root_client(self, state_dir, capsys):
        """F1. `--root-userns`'s premise is a property of the engine, not of
        this process: a non-root client can be pointed at a rootful podman
        service (CONTAINER_HOST, an ssh connection), where container root is
        host root. The old gate asked only `os.geteuid() == 0` and let that
        through with IS_SANDBOX=1."""
        rc = _run_main(
            ["--root-userns"],
            ImageReadiness(available=True, materialized=True, image_id=IMAGE_ID),
            euid=1000,
            engine_rootless=False,
        )
        out = capsys.readouterr()
        assert rc == 2
        assert "rootful" in (out.out + out.err)

    def test_an_unanswerable_engine_is_refused_rather_than_assumed(self, state_dir, capsys):
        """F1. `podman info` not answering means the premise is unestablished,
        which is not the same as satisfied — and IS_SANDBOX=1 hangs off this
        branch, so the ambiguous case must refuse."""
        rc = _run_main(
            ["--root-userns"],
            ImageReadiness(available=True, materialized=True, image_id=IMAGE_ID),
            euid=1000,
            engine_rootless=None,
        )
        out = capsys.readouterr()
        assert rc == 2
        assert "could not establish" in (out.out + out.err)

    def test_a_rootless_engine_still_passes(self, state_dir):
        """F1 positive control: the fix refuses more, not everything."""
        rc = _run_main(
            ["--root-userns"],
            ImageReadiness(available=True, materialized=True, image_id=IMAGE_ID),
            euid=1000,
            engine_rootless=True,
        )
        assert rc == 0

    def test_the_engine_probe_asks_the_engine_not_the_client(self):
        """F1. `podman info` reports the service's own security context, which
        is the whole point of asking it rather than reading euid."""


        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="false\n", stderr="")

        with patch.object(runtime_mod.subprocess, "run", fake_run):
            assert runtime_mod.engine_is_rootless("podman") is False
        assert seen["cmd"][:2] == ["podman", "info"]
        assert "{{.Host.Security.Rootless}}" in seen["cmd"]

    @pytest.mark.parametrize(
        "returncode,stdout,expected",
        [(0, "true", True), (0, "false", False), (0, "", None), (1, "true", None)],
    )
    def test_the_engine_probe_maps_answers_conservatively(self, returncode, stdout, expected):
        """F1. Anything that is not an explicit `true`/`false` is None, so the
        caller cannot mistake a failed probe for a rootless engine."""


        with patch.object(
            runtime_mod.subprocess, "run",
            return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""),
        ):
            assert runtime_mod.engine_is_rootless("podman") is expected

    # -- F2: the namespace for a root container user must be explicit --------

    @pytest.mark.parametrize("user", ["0:0", "root", "0"])
    def test_a_root_container_user_names_its_namespace(self, user):
        """F2. Omitting --userns does not select podman's default: podman
        consults PODMAN_USERNS and then containers.conf's `userns` first. An
        ambient `keep-id` there would reinstate the whole-rootfs chown lmer
        just announced it skipped, and map container root to a subordinate
        uid rather than the invoking one."""


        args = runtime_mod.base_run_args("podman", False, user, [])
        assert "--userns=host" in args
        assert "--userns=keep-id" not in args

    def test_a_non_root_container_user_still_gets_keep_id(self):
        """F2 positive control."""


        args = runtime_mod.base_run_args("podman", False, "developer", [])
        assert "--userns=keep-id" in args
        assert "--userns=host" not in args

    @pytest.mark.parametrize("user", ["developer", "0:0"])
    def test_docker_gets_no_namespace_flag(self, user):
        """F2. keep-id and this whole question are podman mechanisms."""


        args = runtime_mod.base_run_args("docker", False, user, [])
        assert not [a for a in args if a.startswith("--userns")]

    # -- F3: an unreadable marker cache must not abort a launch --------------

    def test_an_unsearchable_marker_dir_is_unknown_not_fatal(self, state_dir):
        """F3. Path.exists() propagates EACCES — pathlib swallows ENOENT and
        friends, not permission denial — so a mode-000 ~/.lmer/remapped turned
        an advisory cache into a session prerequisite."""
        marker_dir = state_dir / "remapped"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_dir.chmod(0)
        try:
            assert keep_id.remap_recorded(IMAGE_ID) is False
        finally:
            marker_dir.chmod(0o700)

    def test_an_unsearchable_marker_dir_does_not_stop_the_launch(self, state_dir, capsys):
        """F3, end to end: the launch proceeds and prints the notice, which is
        the conservative direction this module already commits to."""
        marker_dir = state_dir / "remapped"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_dir.chmod(0)
        try:
            rc = _run_main(
                [], ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID),
            )
            out = capsys.readouterr()
            assert rc == 0
            assert NOTICE in out.out + out.err
        finally:
            marker_dir.chmod(0o700)

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root ignores the mode bit this test relies on"
    )
    def test_a_searchable_but_unwritable_marker_dir_is_still_best_effort(self, state_dir):
        """F3 control: the defect is denied *lookup*, not every read-only
        cache. A 0500 directory answered correctly before the fix and still
        does."""
        marker_dir = state_dir / "remapped"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_dir.chmod(0o500)
        try:
            assert keep_id.remap_recorded(IMAGE_ID) is False
            keep_id.record_remap(IMAGE_ID)
            assert keep_id.remap_recorded(IMAGE_ID) is False
        finally:
            marker_dir.chmod(0o700)

    # -- F5: a signal-killed client is not evidence of a completed remap -----

    @pytest.mark.parametrize("status", [-9, -15])
    def test_a_signal_killed_runtime_does_not_mark_the_image_warm(self, state_dir, status):
        """F5. subprocess spells "the child died on a signal" as a negative
        return code. Killed mid-remap, that says nothing about whether the
        chown finished, so it must not silence the next launch's notice."""
        _run_main(
            [], ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID),
            status=status,
        )
        assert keep_id.remap_recorded(IMAGE_ID) is False

    def test_a_completed_launch_still_marks_the_image_warm(self, state_dir):
        """F5 positive control."""
        _run_main(
            [], ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID),
            status=0,
        )
        assert keep_id.remap_recorded(IMAGE_ID) is True

    # -- F6: warm and record the same resolved ID ----------------------------

    def test_prewarm_runs_the_id_it_records(self, state_dir, monkeypatch):
        """F6. Resolving a tag and then running that tag leaves a window: a
        concurrent rebuild retagging in between warms the new image and
        records the old one, certifying a retained image nothing remapped."""
        ran = []
        monkeypatch.setattr(keep_id, "image_id", lambda *a, **k: IMAGE_ID)
        monkeypatch.setattr(
            keep_id.subprocess, "call",
            lambda cmd, **kw: ran.append(cmd[-1]) or 0,
        )
        assert keep_id.prewarm("podman", "lmer:dev-abc") is True
        assert ran == [IMAGE_ID]
        assert keep_id.remap_recorded(IMAGE_ID) is True

    def test_a_retag_between_inspect_and_run_cannot_certify_an_unrun_image(
        self, state_dir, monkeypatch,
    ):
        """F6, the race itself: another builder publishes a replacement under
        the same tag after the inspect. Running the ID means the retag cannot
        redirect what was warmed."""
        other = "sha256:" + "b" * 64
        tag_now = {"value": IMAGE_ID}
        ran = []
        monkeypatch.setattr(keep_id, "image_id", lambda *a, **k: tag_now["value"])

        def retag_then_run(cmd, **kwargs):
            tag_now["value"] = other  # the concurrent rebuild lands here
            ran.append(cmd[-1])
            return 0

        monkeypatch.setattr(keep_id.subprocess, "call", retag_then_run)
        assert keep_id.prewarm("podman", "lmer:dev-abc") is True
        assert ran == [IMAGE_ID], "warmed something other than the ID it resolved"
        assert keep_id.remap_recorded(IMAGE_ID) is True
        assert keep_id.remap_recorded(other) is False, "certified an image it never ran"

    def test_an_unresolvable_id_falls_back_to_the_tag_and_claims_nothing(
        self, state_dir, monkeypatch,
    ):
        """F6. With no ID there is nothing to bind to, so warm the tag and
        record nothing rather than skip the warm entirely."""
        ran = []
        monkeypatch.setattr(keep_id, "image_id", lambda *a, **k: None)
        monkeypatch.setattr(
            keep_id.subprocess, "call",
            lambda cmd, **kw: ran.append(cmd[-1]) or 0,
        )
        assert keep_id.prewarm("podman", "lmer:dev-abc") is True
        assert ran == ["lmer:dev-abc"]
        assert not list((state_dir / "remapped").glob("*")) if (state_dir / "remapped").exists() else True


class TestBuildReadsHostConfig:
    """F4 of the !309 adversarial review: `build` is dispatched before main()'s
    .env load, so the settings that now steer a build were honoured from an
    exported shell variable but ignored from the ~/.lmer/.env the docs point
    users at."""

    def _run_build(self, tmp_path, monkeypatch, env_text, image_id=IMAGE_ID):
        from lmer_cli import cli

        (tmp_path / ".env").write_text(env_text)
        (tmp_path / "Containerfile").touch()
        monkeypatch.chdir(tmp_path)
        calls = []
        # apply_env_file_defaults mutates os.environ, and `lmer build` exits
        # right after — in-process that would leak into the next test.
        with patch.dict(os.environ, {}), \
             patch.object(cli, "detect_runtime", return_value="podman"), \
             patch.object(cli, "repo_root_path", return_value=tmp_path), \
             patch.object(cli, "resolve_image_tag", return_value="lmer:dev-test"), \
             patch.object(cli, "lmer_state_dir", return_value=tmp_path / "state"), \
             patch("lmer_cli.build.build_image_local", return_value=0), \
             patch.object(keep_id, "image_id", return_value=image_id), \
             patch.object(keep_id.subprocess, "call",
                          lambda cmd, **kw: calls.append(cmd) or 0):
            rc = cli.main(["build"])
        return rc, calls

    @pytest.mark.parametrize(
        "setting",
        ["LMER_NO_PREWARM=1", "LMER_ROOT_USERNS=1", "LMER_CONTAINER_USER=root"],
    )
    def test_a_dotenv_opt_out_is_honoured_by_build(self, tmp_path, monkeypatch, state_dir, setting):
        rc, calls = self._run_build(tmp_path, monkeypatch, setting + "\n")
        assert rc == 0
        assert calls == [], f"{setting} in .env still ran the pre-warm: {calls}"

    def test_without_an_opt_out_build_still_prewarms(self, tmp_path, monkeypatch, state_dir):
        """Positive control: the fix reads the file, it does not disable the
        pre-warm."""
        rc, calls = self._run_build(tmp_path, monkeypatch, "LMER_UNRELATED=1\n")
        assert rc == 0
        assert len(calls) == 1
        assert "--userns=keep-id" in calls[0]

    def test_an_exported_variable_still_beats_the_file(self, tmp_path, monkeypatch, state_dir):
        """First-wins, same as a launch: the .env load must not overwrite an
        already-exported value."""
        monkeypatch.setenv("LMER_NO_PREWARM", "0")
        rc, calls = self._run_build(tmp_path, monkeypatch, "LMER_NO_PREWARM=1\n")
        assert rc == 0
        assert len(calls) == 1, "the .env overrode an exported variable"


class TestVerifyScriptEffectiveTarget:
    """F7 of the !309 adversarial review: the --image mismatch check settled
    only half the question. `lmer build` also honours $LMER_IMAGE and builds
    with whichever runtime detect_runtime() picks, so the script could rebuild
    one image or store and time another — and a pre-existing warm image
    supplies a green number for it."""

    def _bin(self, tmp_path):
        bin_dir, calls, marker = _stub_bin(tmp_path)
        build_log = tmp_path / "lmer-build.log"
        _write_exec(bin_dir / "lmer", f'''#!/usr/bin/env bash
echo "$@" >> "{build_log}"
exit 0
''')
        return bin_dir, calls, build_log, marker

    def test_a_docker_build_is_refused_before_the_rebuild(self, tmp_path):
        """Both runtimes installed means lmer picks docker while this script
        times podman — separate image stores, so the number would be about an
        image the build never touched."""
        bin_dir, _, build_log, _ = self._bin(tmp_path)
        venv_python = _lmer_venv_python(tmp_path)
        result = _run_script(
            bin_dir,
            ["--measure-build", "--python", str(venv_python)],
            extra_env={"STUB_RUNTIME": "docker"},
        )
        assert result.returncode == 2
        assert "docker" in result.stderr and "podman" in result.stderr
        assert not build_log.exists(), "rebuilt before deciding it could not measure"

    def test_an_unresolvable_runtime_is_refused_before_the_rebuild(self, tmp_path):
        bin_dir, _, build_log, _ = self._bin(tmp_path)
        venv_python = _lmer_venv_python(tmp_path)
        result = _run_script(
            bin_dir,
            ["--measure-build", "--python", str(venv_python)],
            extra_env={"STUB_RUNTIME": ""},
        )
        assert result.returncode == 2
        assert "runtime" in result.stderr
        assert not build_log.exists()

    def test_lmer_image_redirects_the_measurement_with_the_build(self, tmp_path):
        """$LMER_IMAGE is what `lmer build` targets, so it is what must be
        measured — previously the script timed the input-hash tag regardless."""
        bin_dir, calls, build_log, marker = self._bin(tmp_path)
        venv_python = _lmer_venv_python(tmp_path)
        result = _run_script(
            bin_dir,
            ["--measure-build", "--python", str(venv_python)],
            extra_env={"LMER_IMAGE": "lmer:override"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert build_log.exists()
        timed = [ln for ln in calls.read_text().splitlines() if ln.startswith("run ")]
        assert len(timed) == 1
        assert timed[0].split()[-1] == "lmer:override", (
            f"built lmer:override but timed {timed[0].split()[-1]}"
        )
        assert marker not in result.stdout.split("measuring:")[1].splitlines()[0]

    def test_an_image_flag_is_checked_against_the_effective_target(self, tmp_path):
        """--image must agree with $LMER_IMAGE when that is what the build
        will produce, not with resolve_image_tag."""
        bin_dir, _, build_log, marker = self._bin(tmp_path)
        venv_python = _lmer_venv_python(tmp_path)
        result = _run_script(
            bin_dir,
            ["--measure-build", "--python", str(venv_python), "--image", marker],
            extra_env={"LMER_IMAGE": "lmer:override"},
        )
        assert result.returncode == 2
        assert "lmer:override" in result.stderr
        assert not build_log.exists()


class TestLaunchBindsToTheResolvedId:
    """F6's launch half (!309 review round 6). `prewarm` binds warming and
    recording to one resolved ID; the launch did not — it read and recorded
    `ensure_image`'s ID while running the mutable tag, so a retag inside the
    session-setup window certified an image podman never ran.
    """

    def _launch(self, monkeypatch, state_dir, *, resolved, runtime="podman",
                on_run=None, status=0):
        """Drive main() to the container call and report what it did."""
        import os as _os

        from tests.test_lmer_cli_slack_target import (
            _BASE_ENV, _make_main_mocks, REPO_URL,
        )
        from lmer_cli import cli

        seen = {}

        def fake_call(cmd, **kwargs):
            seen["argv"] = list(cmd)
            if on_run is not None:
                on_run()
            return status

        argv = ["chat", "--no-task", "--exec", "true", REPO_URL]
        readiness = ImageReadiness(available=True, materialized=False, image_id=IMAGE_ID)
        with patch.dict(_os.environ, _BASE_ENV, clear=True):
            with _make_main_mocks():
                with patch.object(cli, "detect_runtime", return_value=runtime), \
                     patch.object(cli.os, "geteuid", return_value=1000), \
                     patch.object(cli, "ensure_image", MagicMock(return_value=readiness)), \
                     patch.object(cli, "remap_image_id", side_effect=resolved), \
                     patch.object(cli.subprocess, "call", fake_call):
                    seen["rc"] = cli.main(argv)
        return seen

    def test_the_argv_tail_names_the_resolved_id_not_the_tag(self, monkeypatch, state_dir):
        """The reviewer's own check: the image reference podman is handed."""
        late = "sha256:" + "c" * 64
        seen = self._launch(monkeypatch, state_dir, resolved=lambda *a: late)
        assert seen["rc"] == 0
        argv = seen["argv"]
        assert late in argv, f"argv does not name the resolved ID: {argv[-6:]}"
        assert argv.index(late) < len(argv), "resolved ID is not in the image position"
        assert not [a for a in argv if a.endswith(":latest")], (
            f"argv still names a mutable tag: {argv[-6:]}"
        )

    def test_the_marker_read_and_the_record_use_that_same_id(self, monkeypatch, state_dir):
        """One resolution feeds the notice, the container and the record."""
        late = "sha256:" + "c" * 64
        seen = self._launch(monkeypatch, state_dir, resolved=lambda *a: late)
        assert seen["rc"] == 0
        assert keep_id.remap_recorded(late) is True, "recorded an ID other than the one run"
        assert keep_id.remap_recorded(IMAGE_ID) is False, (
            "recorded ensure_image's stale ID"
        )

    def test_a_retag_during_session_setup_cannot_certify_an_unrun_image(
        self, monkeypatch, state_dir,
    ):
        """The scenario itself: another builder retags between ensure_image and
        the launch. The late resolution is what podman is handed, so the marker
        can only ever name the image that actually ran."""
        late = "sha256:" + "c" * 64
        seen = self._launch(monkeypatch, state_dir, resolved=lambda *a: late)
        ran = [a for a in seen["argv"] if a.startswith("sha256:")]
        assert ran == [late]
        assert keep_id.remap_recorded(late) is True
        assert keep_id.remap_recorded(IMAGE_ID) is False, (
            "the stale pre-setup ID was certified warm by a run of a different image"
        )

    def test_the_notice_is_decided_about_the_image_that_will_run(
        self, monkeypatch, state_dir, capsys,
    ):
        """A warm stale ID must not silence the notice for the cold image the
        launch is actually about."""
        late = "sha256:" + "c" * 64
        keep_id.record_remap(IMAGE_ID)  # ensure_image's ID is warm...
        seen = self._launch(monkeypatch, state_dir, resolved=lambda *a: late)
        out = capsys.readouterr()
        assert seen["rc"] == 0
        assert NOTICE in out.out + out.err, (
            "a warm stale ID silenced the notice for the cold image that ran"
        )

    def test_an_unresolvable_id_falls_back_to_the_tag(self, monkeypatch, state_dir):
        """No answer from `image inspect` leaves behaviour where it was: run the
        tag, and fall back to ensure_image's ID for the marker."""
        seen = self._launch(monkeypatch, state_dir, resolved=lambda *a: None)
        assert seen["rc"] == 0
        assert not [a for a in seen["argv"] if a.startswith("sha256:")]
        assert keep_id.remap_recorded(IMAGE_ID) is True

    def test_docker_neither_re_resolves_nor_rewrites_the_argv(self, monkeypatch, state_dir):
        """keep-id is a podman mechanism: docker pays no extra inspect and its
        argv is untouched."""
        calls = []

        def resolved(*a):
            calls.append(a)
            return "sha256:" + "c" * 64

        seen = self._launch(monkeypatch, state_dir, resolved=resolved, runtime="docker")
        assert seen["rc"] == 0
        assert calls == [], "docker paid an image inspect it has no use for"
        assert not [a for a in seen["argv"] if a.startswith("sha256:")]
