"""The spawn's half of the status document's mount (issue #248).

The document reaches the platform only if both halves hold: the directory is
bind-mounted in, and the session is *told* where that mount landed. Either
alone is a silent failure, so they are asserted together and through the mount,
as ``tests/test_platform_spawn`` does for the ask channel and the upload store.

The stub stands in for the child ``lmer`` and the harness inside it: it finds
the mount through the variable, exactly as the renderer does, and writes back
both what it was told and a document.
"""

import json
import time

import pytest

from lmer_cli.cli import parse_dir_mount_specs
from lmer_cli.user_harnesses import (
    HARNESSES_DIR_ENV, clear_user_harness_cache,
)
from lmer_platform import config as cfg
from lmer_platform import registry, spawn, statusline, store
from tests.conftest import strip_lmer_env

#: What the stub publishes, in the shape the renderer publishes it.
PUBLISHED = {
    "schema": 1,
    "harness": "claude",
    "updated": "2026-09-03T14:31:07Z",
    "context": {"percent": 9},
    "limits": [{"key": "five_hour", "label": "5h", "percent": 24}],
    "effort": {"level": "high"},
}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_REPO_URL", "LMER_PLATFORM_PORTS_FILE", "LMER_STATUS_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_user_harnesses(tmp_path, monkeypatch, _clean_lmer_env):
    """The mounts a spawn builds are this checkout's built-ins, never whatever
    the developer running the suite keeps in ``~/.lmer/harnesses``."""
    root = tmp_path / "harnesses"
    root.mkdir()
    monkeypatch.setenv(HARNESSES_DIR_ENV, str(root))
    clear_user_harness_cache()
    yield root
    clear_user_harness_cache()


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    """A stub `lmer` that publishes a status document through the mount.

    It resolves the host directory the way the renderer resolves the container
    one — by the destination the platform named — and records the variable's
    value beside the document, so "mounted" and "told about it" are one
    assertion.
    """
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'spec=""; prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "--mount-dir" ]; then\n'
        '    case "$arg" in\n'
        f'      *:{statusline.CONTAINER_STATUS_DIR}:*) spec="$arg" ;;\n'
        "    esac\n"
        "  fi\n"
        '  prev="$arg"\n'
        "done\n"
        'host="${spec%%:*}"\n'
        'if [ -n "$host" ] && [ -d "$host" ]; then\n'
        '  printf "%s" "${LMER_STATUS_DIR-<unset>}" > "$host/child-saw-env"\n'
        '  if [ -n "$FAKE_LMER_STATUS" ]; then\n'
        f'    printf "%s" "$FAKE_LMER_STATUS" > "$host/{statusline.STATUS_FILENAME}"\n'
        "  fi\n"
        "fi\n"
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        'exit "${FAKE_LMER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


def request_for(**overrides):
    payload = {
        "taskdef": "develop",
        "target": "https://gitlab.example.com/agents/global/-/work_items/248",
        "repo_url": "https://gitlab.example.com/agents/global.git",
    }
    payload.update(overrides)
    return spawn.SpawnRequest(**payload)


def wait_for(predicate, timeout=10.0):
    """Poll *predicate* until it holds. False on timeout.

    A spawn returns once the child is started, not once it has run: the stub
    publishes its document a moment later, and asserting straight away is a race
    the suite loses under load rather than a defect in the code under test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def status_mount_spec(command):
    """The single ``--mount-dir`` value aimed at the status directory."""
    specs = [
        command[index + 1]
        for index, arg in enumerate(command)
        if arg == "--mount-dir" and index + 1 < len(command)
    ]
    matching = [
        spec for spec in specs
        if spec.split(":")[1:2] == [statusline.CONTAINER_STATUS_DIR]
    ]
    assert len(matching) == 1, f"expected one status mount, got {specs}"
    return matching[0]


class TestTheMount:
    def test_the_directory_exists_before_the_session_starts(self, config):
        """The container mounts it; a directory created afterwards is too late."""
        result = spawn.spawn_session(config, request_for())
        assert spawn.status_dir_for(result.session_id).is_dir()

    def test_it_is_mounted_read_write_where_the_writer_is_told(self, config):
        """``rw`` because the harness rewrites the document on every render, and
        at the same constant the variable names — the mount and what the writer
        reads are one fact rather than two that agree today."""
        result = spawn.spawn_session(config, request_for())
        specs = parse_dir_mount_specs([status_mount_spec(result.command)], "")
        assert len(specs) == 1
        assert specs[0].host == spawn.status_dir_for(result.session_id)
        assert specs[0].container == statusline.CONTAINER_STATUS_DIR
        assert specs[0].mode == "rw"

    def test_the_mount_precedes_a_container_command(self, config):
        """Everything after ``--`` is the container's command line: appended
        there, the flag is an argument to ``echo`` and the mount silently does
        not happen."""
        result = spawn.spawn_session(
            config, request_for(extra_args=("--", "echo", "hi"))
        )
        assert result.command.index("--mount-dir") < result.command.index("--")

    def test_the_child_is_told_the_container_path(self, config):
        result = spawn.spawn_session(config, request_for())
        assert spawn.wait_for_exit_recorded(result.session_id, timeout=5)
        seen = spawn.status_dir_for(result.session_id) / "child-saw-env"
        assert seen.read_text() == statusline.CONTAINER_STATUS_DIR

    def test_a_worker_and_the_assistant_both_get_one(self, config):
        """Which harness the child resolves to is decided inside it, so every
        session is offered the directory — an empty one costs nothing and a
        missing one costs the strip."""
        for index, kind in enumerate(
            (registry.WORKER_KIND, registry.ASSISTANT_KIND)
        ):
            # A target apiece: two sessions on one run is what RunAlreadyLive
            # exists to refuse, and this test is about the kinds.
            result = spawn.spawn_session(
                config,
                request_for(target=f"https://gitlab.example.com/agents/global/"
                                   f"-/work_items/{248 + index}"),
                kind=kind,
            )
            assert spawn.status_dir_for(result.session_id).is_dir()
            assert status_mount_spec(result.command)


class TestWithoutADirectory:
    @pytest.fixture
    def unusable(self, monkeypatch):
        """The platform could not prepare a directory for this session."""
        monkeypatch.setattr(statusline, "prepare_status_dir", lambda session_id: None)

    def test_nothing_is_mounted(self, config, unusable):
        result = spawn.spawn_session(config, request_for())
        specs = [
            arg for arg in result.command
            if statusline.CONTAINER_STATUS_DIR in str(arg)
        ]
        assert specs == []

    def test_the_variable_is_blank_rather_than_absent(self, config, unusable, tmp_path):
        """The child is ``lmer``, which seeds its own environment from .env
        files first-wins — so a key that is merely deleted is a key a file may
        re-supply, pointing the harness at a directory this session has not
        got (the #260 shape)."""
        recorded = tmp_path / "child-env"
        script = tmp_path / "env-recording-lmer"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s" "${{LMER_STATUS_DIR-<unset>}}" > "{recorded}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        result = spawn.spawn_session(
            cfg.load({"lmer_bin": str(script)}), request_for()
        )
        assert spawn.wait_for_exit_recorded(result.session_id, timeout=5)
        assert recorded.read_text() == ""

    def test_a_daemons_own_variable_is_never_inherited(
        self, config, unusable, monkeypatch,
    ):
        """A daemon started from inside a session carries this variable, and
        passing it down would aim one session's writes at another's mount."""
        monkeypatch.setenv("LMER_STATUS_DIR", "/home/developer/.lmer-status")
        result = spawn.spawn_session(config, request_for())
        assert spawn.wait_for_exit_recorded(result.session_id, timeout=5)
        # Nothing was mounted, so the stub wrote nothing — the directory the
        # inherited value names must not have been offered to the child either.
        assert not (spawn.status_dir_for(result.session_id) / "child-saw-env").exists()


class TestEndToEnd:
    def test_the_platform_reads_what_the_session_published(self, config, monkeypatch):
        """The whole path in one test: mount, variable, document, read."""
        monkeypatch.setenv("FAKE_LMER_STATUS", json.dumps(PUBLISHED))
        monkeypatch.setenv("FAKE_LMER_SLEEP", "5")
        result = spawn.spawn_session(config, request_for())
        assert wait_for(lambda: statusline.for_session(result.session_id) is not None)
        served = statusline.for_session(result.session_id)
        assert served["context"] == {"percent": 9}
        assert served["limits"][0]["label"] == "5h"
        assert served["effort"] == {"level": "high"}

    def test_it_reaches_the_fleet_path_for_a_live_session(self, config, monkeypatch):
        monkeypatch.setenv("FAKE_LMER_STATUS", json.dumps(PUBLISHED))
        monkeypatch.setenv("FAKE_LMER_SLEEP", "5")
        result = spawn.spawn_session(config, request_for())
        assert wait_for(lambda: result.session_id in statusline.by_session(
            registry.list_sessions(live_only=False)
        ))


class TestCleanup:
    def test_a_clean_exit_takes_the_document_with_it(self, config, monkeypatch):
        monkeypatch.setenv("FAKE_LMER_STATUS", json.dumps(PUBLISHED))
        result = spawn.spawn_session(config, request_for())
        assert spawn.wait_for_exit_recorded(result.session_id, timeout=10)
        assert not statusline.status_file_for(result.session_id).exists()

    def test_a_crash_takes_it_too(self, config, monkeypatch):
        """Unlike the ports file, which a kept entry still has a reader for
        (``absorb_ports``): nothing reads a status document once the session
        that was publishing it has stopped, so a crashed session must not leave
        one behind for good."""
        monkeypatch.setenv("FAKE_LMER_STATUS", json.dumps(PUBLISHED))
        monkeypatch.setenv("FAKE_LMER_EXIT", "1")
        result = spawn.spawn_session(config, request_for())
        assert spawn.wait_for_exit_recorded(result.session_id, timeout=5)
        assert not statusline.status_file_for(result.session_id).exists()
        assert registry.read_session(result.session_id) is not None, (
            "the entry is the crash signal and stays"
        )


class TestTheDestinationIsProtected:
    def test_a_drop_in_cannot_claim_it(self):
        """A user harness declaring this destination would silently take over
        the platform's mount (``--mount-dir`` dedupes last-wins), so the spawn
        knows it is taken."""
        reserved = spawn._reserved_session_destinations()
        key = spawn._container_dir_key(statusline.CONTAINER_STATUS_DIR)
        assert reserved[key] == "the session's status document"

    def test_a_caller_cannot_redirect_it(self, config):
        """And a caller's own ``--mount-dir`` at it is refused rather than
        redirecting the session's publishing somewhere the platform will not
        look."""
        assert statusline.CONTAINER_STATUS_DIR in spawn._protected_mount_destinations()
        with pytest.raises(spawn.SpawnError) as excinfo:
            spawn.spawn_session(config, request_for(extra_args=(
                "--mount-dir", f"/tmp/mine:{statusline.CONTAINER_STATUS_DIR}:rw",
            )))
        assert "status" in str(excinfo.value).lower()
