"""Retargeting a group session inside the container (issue #312).

Two halves, because the feature is two programs: the Python switcher that picks
a member and records it, and the bash wrappers that read that record on every
invocation. The wrappers are *executed* here, not re-implemented — the same way
tests/test_mount_links_script.py runs the real mount-link script — against a
stub ``docker`` on PATH, since asserting on the argv they hand the runtime is
the only way to see which container a command would have run in.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.container.target_switch import (
    DEFAULT_TARGET_FILE,
    main,
    read_target,
    target_file_path,
    write_target,
)
from lmer_cli.service import ServiceError, ServiceMember


REPO_ROOT = Path(__file__).parent.parent
TARGET_EXEC = REPO_ROOT / "bin" / "target-exec"
TARGET_LOGS = REPO_ROOT / "bin" / "target-logs"

MEMBERS = [
    ServiceMember("db", "bbb22222222b", "stack-db-1", "/"),
    ServiceMember("web", "aaa11111111a", "stack-web-1", "/srv/app"),
]


@pytest.fixture
def target_file(tmp_path, monkeypatch):
    path = tmp_path / "session" / "service-target"
    monkeypatch.setenv("LMER_SERVICE_TARGET_FILE", str(path))
    return path


def _stub_docker(tmp_path: Path) -> Path:
    """A `docker` that records its argv instead of talking to a runtime."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "docker"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/docker-argv"\n'
    )
    stub.chmod(0o755)
    return bindir


def _run_wrapper(script: Path, args, env, tmp_path):
    bindir = _stub_docker(tmp_path)
    full = {"PATH": f"{bindir}:/usr/bin:/bin", **env}
    result = subprocess.run(
        ["bash", str(script), *args],
        env=full, capture_output=True, text=True, timeout=30,
    )
    argv_file = tmp_path / "docker-argv"
    argv = argv_file.read_text().splitlines() if argv_file.exists() else []
    return result, argv


class TestPointerFile:
    """The record itself."""

    def test_round_trips(self, target_file):
        write_target(target_file, MEMBERS[1])
        assert read_target(target_file) == ("web", "aaa11111111a", "/srv/app")

    def test_missing_file_reads_as_no_target(self, target_file):
        assert read_target(target_file) is None

    def test_truncated_file_reads_as_no_target(self, target_file):
        """Half a record is not a target — the readers must not guess at it."""
        target_file.parent.mkdir(parents=True)
        target_file.write_text("web\n")
        assert read_target(target_file) is None

    def test_write_is_atomic(self, target_file):
        """A reader mid-switch sees one target or the other, never a blend."""
        write_target(target_file, MEMBERS[0])
        with patch("os.replace") as replace:
            write_target(target_file, MEMBERS[1])
        assert replace.called
        # The visible file is untouched until the replace lands.
        assert read_target(target_file) == ("db", "bbb22222222b", "/")

    def test_no_leftover_temp_files(self, target_file):
        write_target(target_file, MEMBERS[0])
        write_target(target_file, MEMBERS[1])
        assert [p.name for p in target_file.parent.iterdir()] == [
            target_file.name
        ]

    def test_default_path_is_used_without_the_env_var(self, monkeypatch):
        monkeypatch.delenv("LMER_SERVICE_TARGET_FILE", raising=False)
        assert str(target_file_path()) == DEFAULT_TARGET_FILE


class TestSwitcher:
    """`target-switch` itself."""

    def _env(self, monkeypatch, group="stack"):
        if group is None:
            monkeypatch.delenv("LMER_SERVICE_GROUP", raising=False)
        else:
            monkeypatch.setenv("LMER_SERVICE_GROUP", group)

    def test_switch_records_the_member(self, target_file, monkeypatch):
        self._env(monkeypatch)
        with patch(
            "lmer_cli.container.target_switch.resolve_group",
            return_value=MEMBERS,
        ):
            assert main(["web"]) == 0
        assert read_target(target_file) == ("web", "aaa11111111a", "/srv/app")

    def test_membership_is_reread_on_every_switch(self, target_file, monkeypatch):
        """A member that restarted under a new id is still reachable."""
        self._env(monkeypatch)
        restarted = [ServiceMember("web", "ccc33333333c", "stack-web-1", "/srv/app")]
        with patch(
            "lmer_cli.container.target_switch.resolve_group",
            return_value=restarted,
        ):
            assert main(["web"]) == 0
        assert read_target(target_file)[1] == "ccc33333333c"

    def test_non_member_is_refused_and_nothing_is_recorded(
        self, target_file, monkeypatch, capsys
    ):
        self._env(monkeypatch)
        with patch(
            "lmer_cli.container.target_switch.resolve_group",
            return_value=MEMBERS,
        ):
            assert main(["cache"]) == 2
        assert read_target(target_file) is None
        err = capsys.readouterr().err
        assert "cache" in err and "web" in err

    def test_listing_names_the_group_and_the_current_target(
        self, target_file, monkeypatch, capsys
    ):
        self._env(monkeypatch)
        write_target(target_file, MEMBERS[1])
        with patch(
            "lmer_cli.container.target_switch.resolve_group",
            return_value=MEMBERS,
        ):
            assert main([]) == 0
        out = capsys.readouterr().out
        assert "stack" in out
        assert "web" in out and "db" in out
        assert "target-switch <service>" in out

    def test_listing_says_so_when_nothing_is_selected(
        self, target_file, monkeypatch, capsys
    ):
        self._env(monkeypatch)
        monkeypatch.delenv("LMER_SERVICE_CONTAINER", raising=False)
        with patch(
            "lmer_cli.container.target_switch.resolve_group",
            return_value=MEMBERS,
        ):
            assert main([]) == 0
        assert "none selected yet" in capsys.readouterr().out

    def test_outside_a_group_it_refuses(self, target_file, monkeypatch, capsys):
        self._env(monkeypatch, group=None)
        assert main(["web"]) == 2
        assert "not attached to a service group" in capsys.readouterr().err

    def test_unreachable_group_is_reported_not_recorded(
        self, target_file, monkeypatch, capsys
    ):
        self._env(monkeypatch)
        with patch(
            "lmer_cli.container.target_switch.resolve_group",
            side_effect=ServiceError("No running containers in compose project"),
        ):
            assert main(["web"]) == 2
        assert read_target(target_file) is None

    def test_only_the_switched_replica_is_marked_current(
        self, target_file, monkeypatch, capsys
    ):
        """Replicas share one service name, so a name-keyed marker claimed every
        sibling was the target. The pointer file records the container id — that
        is what identifies the row."""
        self._env(monkeypatch)
        replicas = [
            ServiceMember("web", "aaa11111111a", "stack-web-1", "/srv/app"),
            ServiceMember("web", "bbb22222222b", "stack-web-2", "/srv/app"),
            ServiceMember("db", "ccc33333333c", "stack-db-1", "/"),
        ]
        with patch(
            "lmer_cli.container.target_switch.resolve_group",
            return_value=replicas,
        ):
            assert main(["stack-web-2"]) == 0
            capsys.readouterr()
            assert main([]) == 0

        marked = [
            line for line in capsys.readouterr().out.splitlines()
            if line.startswith("  *")
        ]
        assert len(marked) == 1, f"expected one marked row, got {marked}"
        assert "stack-web-2" in marked[0]

    def test_a_recreated_target_leaves_no_row_marked_and_says_so(
        self, target_file, monkeypatch, capsys
    ):
        """The other side of an id-keyed marker: once the recorded container is
        gone, nothing matches — which has to be stated, or an unmarked listing
        reads as a broken marker."""
        self._env(monkeypatch)
        before = [ServiceMember("web", "aaa11111111a", "stack-web-1", "/srv/app")]
        after = [ServiceMember("web", "ddd44444444d", "stack-web-1", "/srv/app")]
        with patch(
            "lmer_cli.container.target_switch.resolve_group", return_value=before
        ):
            assert main(["web"]) == 0
        capsys.readouterr()
        with patch(
            "lmer_cli.container.target_switch.resolve_group", return_value=after
        ):
            assert main([]) == 0

        out = capsys.readouterr().out
        assert not [line for line in out.splitlines() if line.startswith("  *")]
        assert "no longer running" in out

    def test_more_than_one_name_is_a_usage_error(self, target_file, monkeypatch):
        self._env(monkeypatch)
        assert main(["web", "db"]) == 2


class TestWrappersFollowTheSwitch:
    """The bash wrappers, executed for real."""

    def test_target_exec_uses_the_pointer_file(self, tmp_path):
        target = tmp_path / "service-target"
        target.write_text("web\naaa11111111a\n/srv/app\n")
        result, argv = _run_wrapper(
            TARGET_EXEC, ["pytest", "-x"],
            {
                "LMER_SERVICE_TARGET_FILE": str(target),
                # The launch environment still names the *other* member: the
                # switch has to win, or a retarget would silently do nothing.
                "LMER_SERVICE_CONTAINER": "bbb22222222b",
                "LMER_SERVICE_WORKDIR": "/",
                "LMER_SERVICE_GROUP": "stack",
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert argv == ["exec", "-w", "/srv/app", "aaa11111111a", "pytest", "-x"]

    def test_target_exec_falls_back_to_the_launch_env(self, tmp_path):
        """No pointer file: single-service sessions behave exactly as before."""
        result, argv = _run_wrapper(
            TARGET_EXEC, ["pytest"],
            {
                "LMER_SERVICE_TARGET_FILE": str(tmp_path / "absent"),
                "LMER_SERVICE_CONTAINER": "bbb22222222b",
                "LMER_SERVICE_WORKDIR": "/app",
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert argv == ["exec", "-w", "/app", "bbb22222222b", "pytest"]

    def test_target_exec_without_a_target_names_the_switch(self, tmp_path):
        result, argv = _run_wrapper(
            TARGET_EXEC, ["pytest"],
            {
                "LMER_SERVICE_TARGET_FILE": str(tmp_path / "absent"),
                "LMER_SERVICE_GROUP": "stack",
            },
            tmp_path,
        )
        assert result.returncode == 1
        assert "no service selected yet" in result.stderr
        assert "target-switch" in result.stderr
        assert argv == []

    def test_target_exec_refuses_a_malformed_pointer_file(self, tmp_path):
        """Never silently exec somewhere other than what the agent chose."""
        target = tmp_path / "service-target"
        target.write_text("web\n")
        result, argv = _run_wrapper(
            TARGET_EXEC, ["pytest"],
            {
                "LMER_SERVICE_TARGET_FILE": str(target),
                "LMER_SERVICE_CONTAINER": "bbb22222222b",
            },
            tmp_path,
        )
        assert result.returncode == 1
        assert "malformed" in result.stderr
        assert argv == []

    def test_target_exec_does_not_source_the_pointer_file(self, tmp_path):
        """The file is read, never evaluated."""
        target = tmp_path / "service-target"
        canary = tmp_path / "canary"
        target.write_text(
            f"web\naaa11111111a\n/srv/app\n$(touch {canary})\n"
        )
        result, _ = _run_wrapper(
            TARGET_EXEC, ["true"],
            {"LMER_SERVICE_TARGET_FILE": str(target)},
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert not canary.exists()

    def test_target_logs_follows_the_switch(self, tmp_path):
        target = tmp_path / "service-target"
        target.write_text("web\naaa11111111a\n/srv/app\n")
        result, argv = _run_wrapper(
            TARGET_LOGS, [],
            {
                "LMER_SERVICE_TARGET_FILE": str(target),
                "LMER_SERVICE_CONTAINER": "bbb22222222b",
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert argv == ["logs", "--tail", "50", "-f", "aaa11111111a"]

    def test_target_logs_reaches_an_explicit_container_past_a_bad_file(self, tmp_path):
        """The escape hatch has to work when the pointer file is what is broken:
        the caller named the container, so nothing about the file matters."""
        target = tmp_path / "service-target"
        target.write_text("web\n")
        result, argv = _run_wrapper(
            TARGET_LOGS, ["other-container", "10"],
            {
                "LMER_SERVICE_TARGET_FILE": str(target),
                "LMER_SERVICE_CONTAINER": "bbb22222222b",
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert argv == ["logs", "--tail", "10", "-f", "other-container"]

    def test_target_logs_refuses_a_malformed_pointer_file_without_an_argument(
        self, tmp_path
    ):
        """With no explicit container the file *is* the answer, so a broken one
        is an error naming it rather than a fall-back to the launch container."""
        target = tmp_path / "service-target"
        target.write_text("web\n")
        result, argv = _run_wrapper(
            TARGET_LOGS, [],
            {
                "LMER_SERVICE_TARGET_FILE": str(target),
                "LMER_SERVICE_CONTAINER": "bbb22222222b",
            },
            tmp_path,
        )
        assert result.returncode == 1
        assert "malformed" in result.stderr
        assert "target-logs <container>" in result.stderr
        assert argv == []

    def test_target_logs_still_takes_an_explicit_container(self, tmp_path):
        result, argv = _run_wrapper(
            TARGET_LOGS, ["other-container", "10"],
            {
                "LMER_SERVICE_TARGET_FILE": str(tmp_path / "absent"),
                "LMER_SERVICE_CONTAINER": "bbb22222222b",
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert argv == ["logs", "--tail", "10", "-f", "other-container"]


class TestWrapperScriptsShip:
    """The switch is only usable if the image puts it on PATH."""

    def test_target_switch_is_executable(self):
        script = REPO_ROOT / "bin" / "target-switch"
        assert script.exists()
        assert os.access(script, os.X_OK)

    def test_target_switch_runs_the_module(self):
        source = (REPO_ROOT / "bin" / "target-switch").read_text()
        assert "lmer_cli.container.target_switch" in source
        assert "LMER_PYTHON" in source

    def test_the_wrappers_default_to_the_path_the_switcher_writes(self):
        """Two languages, one path: the bash fallback and the Python constant
        have to name the same file or a switch would be written where nothing
        reads it. Pinned because only a group launch sets the env var that
        would otherwise paper over the mismatch."""
        for script in (TARGET_EXEC, TARGET_LOGS):
            assert f'LMER_SERVICE_TARGET_FILE:-{DEFAULT_TARGET_FILE}' in (
                script.read_text()
            ), f"{script.name} does not default to {DEFAULT_TARGET_FILE}"


class TestTheAgentMayRunIt:
    """The switch is the feature's only interface; a permission prompt on it is
    a stop, not a nuisance — an orchestrated group session cannot answer one and
    stays on whatever member it launched with, or on nothing at all.

    Only the claude harness has a per-command allowlist: codex takes its sandbox
    mode and approval policy as runner flags and pi's settings carry no
    permissions at all, so there is nothing to add on those sides.
    """

    ALLOWLIST = REPO_ROOT / "agent-files" / "claude" / "settings.json"

    def _allow(self) -> list:
        import json

        return json.loads(self.ALLOWLIST.read_text())["permissions"]["allow"]

    @pytest.mark.parametrize("entry", [
        # Bare and argument forms both, because `target-switch` with no
        # argument is a documented invocation (it lists the group) — the same
        # reason target-logs carries both.
        "Bash(target-switch)",
        "Bash(target-switch:*)",
    ])
    def test_target_switch_is_allowlisted(self, entry):
        assert entry in self._allow(), (
            f"{entry} missing from agent-files/claude/settings.json — the "
            "taskdef tells the agent to run it"
        )

    def test_the_other_wrappers_stay_allowlisted(self):
        """Guarding the neighbours too: they are the same contract."""
        allow = self._allow()
        for entry in ("Bash(target-exec:*)", "Bash(target-logs)",
                      "Bash(target-logs:*)"):
            assert entry in allow
