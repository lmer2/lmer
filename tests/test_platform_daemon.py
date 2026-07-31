"""Tests for `lmer platform` (issue #141, slice M1 / T6).

The diagnostic subcommands carry most of the weight here: when the fleet view
looks wrong, `status` and `rescan` are what an operator reaches for, so they must
work without a server, without a token, and without a healthy mirror.
"""

import json
import os

import pytest

from lmer_platform import config as cfg
from lmer_platform import daemon, store, workrepo
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _no_boot_threads(monkeypatch):
    """`lmer platform run` starts two things these tests must not start for real.

    The assistant auto-start (T63) is a real spawn: an unpatched ``run`` here would
    launch the ``lmer`` on PATH — and its container — from the test suite. The
    detection tick (T69) is a thread that pulls the work repo and writes digests,
    which would outlive the test that started it and interleave with the next one.

    Both are stubbed at the seam the daemon calls rather than at the machinery
    beneath, so what is skipped is exactly two lines of wiring — asserted where
    each belongs, against real objects: ``test_platform_assistant_supervision.py``
    and ``test_platform_detection.py``.
    """
    monkeypatch.setattr(daemon, "_supervise_assistant", lambda config: _NoSupervisor())
    monkeypatch.setattr(daemon, "_watch_for_attention", lambda config: _NoSupervisor())


class _NoSupervisor:
    def stop(self):
        pass


# --- argument grammar -------------------------------------------------------

def test_bare_invocation_defaults_to_run(platform_root, monkeypatch):
    served = {}

    def fake_run(args):
        served["log_level"] = args.log_level
        return 0

    monkeypatch.setitem(daemon._COMMANDS, "run", fake_run)
    assert daemon.main([]) == 0
    assert served["log_level"] == "info"


def test_run_accepts_bind_overrides(platform_root, monkeypatch):
    seen = {}

    def fake_run(args):
        seen.update(daemon._overrides(args))
        return 0

    monkeypatch.setitem(daemon._COMMANDS, "run", fake_run)
    daemon.main(["run", "--bind", "0.0.0.0", "--port", "9100"])
    assert seen == {"bind_address": "0.0.0.0", "bind_port": 9100}


def test_unknown_subcommand_exits_nonzero(platform_root):
    with pytest.raises(SystemExit) as excinfo:
        daemon.main(["nonsense"])
    assert excinfo.value.code != 0


def test_config_errors_are_reported_not_raised(platform_root, capsys):
    assert daemon.main(["status", "--port", "0"]) == 2
    assert "outside 1-65535" in capsys.readouterr().err


# --- secret -----------------------------------------------------------------

def test_secret_prints_and_creates(platform_root, capsys):
    assert daemon.main(["secret"]) == 0
    out = capsys.readouterr()
    token = out.out.strip()

    assert len(token) >= 32
    assert cfg.read_secret(cfg.load()) == token
    assert "mode 0600" in out.err


def test_secret_is_stable_across_calls(platform_root, capsys):
    daemon.main(["secret"])
    first = capsys.readouterr().out.strip()
    daemon.main(["secret"])
    assert capsys.readouterr().out.strip() == first


# --- status -----------------------------------------------------------------

def test_status_reports_unconfigured_work_repo(platform_root, capsys):
    assert daemon.main(["status"]) == 0
    out = capsys.readouterr().out

    assert "bind           : http://127.0.0.1:8600" in out
    assert "(not configured)" in out
    assert "mirror         : absent" in out
    assert "runs           : 0" in out


def test_status_json_is_machine_readable(platform_root, capsys):
    assert daemon.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["runs"] == 0
    assert payload["config"]["bind_port"] == 8600


def test_status_honors_bind_overrides(platform_root, capsys):
    daemon.main(["status", "--bind", "10.0.0.5", "--port", "9100"])
    assert "http://10.0.0.5:9100" in capsys.readouterr().out


def test_status_shows_attention_rows(platform_root, capsys, monkeypatch):
    def fake_state(config, *, force_pull=False):
        return {
            "config": {"base_url": "http://127.0.0.1:8600", "work_repo_url": None},
            "mirror": {"present": True, "healthy": True,
                       "last_pull_at": "2026-07-26T12:00:00Z", "last_error": None},
            "totals": {"runs": 1, "live": 0, "attention": 1},
            "counts": {"waiting_on_you": 1},
            "attention": [{
                "label": "lmer-orchestrator",
                "attention": {"reason": "question", "note": "which approach?"},
            }],
        }

    monkeypatch.setattr(daemon, "build_state", fake_state)
    daemon.main(["status"])
    out = capsys.readouterr().out

    assert "1 need you" in out
    assert "waiting_on_you=1" in out
    assert "⚠️  lmer-orchestrator [question] which approach?" in out


def test_status_flags_a_stale_mirror(platform_root, capsys, monkeypatch):
    def fake_state(config, *, force_pull=False):
        return {
            "config": {"base_url": "http://127.0.0.1:8600", "work_repo_url": "u"},
            "mirror": {"present": True, "healthy": False, "last_pull_at": None,
                       "last_error": "fetch failed: unreachable"},
            "totals": {"runs": 0, "live": 0, "attention": 0},
            "counts": {},
            "attention": [],
        }

    monkeypatch.setattr(daemon, "build_state", fake_state)
    daemon.main(["status"])
    out = capsys.readouterr().out

    assert "STALE" in out
    assert "fetch failed: unreachable" in out
    assert "last pull      : never" in out


# --- rescan -----------------------------------------------------------------

def test_rescan_reports_failure_with_nonzero_exit(platform_root, capsys):
    """No work repo configured is a failure the exit code should carry."""
    assert daemon.main(["rescan"]) == 1
    assert "mirror not healthy" in capsys.readouterr().err


def test_rescan_succeeds_against_a_healthy_mirror(platform_root, capsys, monkeypatch):
    healthy = workrepo.MirrorStatus(
        present=True, url="u", last_pull_at="2026-07-26T12:00:00Z",
        last_pull_ok=True, head_sha="abc",
    )
    monkeypatch.setattr(daemon, "pull", lambda config, force=False: healthy)
    monkeypatch.setattr(
        daemon, "build_state",
        lambda config, force_pull=False: {
            "config": {"base_url": "http://127.0.0.1:8600", "work_repo_url": "u"},
            "mirror": healthy.to_dict(),
            "totals": {"runs": 2, "live": 1, "attention": 0},
            "counts": {"running": 1, "complete": 1},
            "attention": [],
        },
    )
    assert daemon.main(["rescan"]) == 0
    assert "runs           : 2 (1 live, 0 need you)" in capsys.readouterr().out


# --- run --------------------------------------------------------------------

def test_run_announces_binding_and_auth(platform_root, capsys, monkeypatch):
    served = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            served.update(kwargs)

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)
    assert daemon.main(["run", "--bind", "0.0.0.0", "--port", "9100"]) == 0

    out = capsys.readouterr().out
    assert "PLAINTEXT" in out, "a network bind must say it is unencrypted"
    assert "lmer platform secret" in out
    assert served["host"] == "0.0.0.0"
    assert served["port"] == 9100
    assert served["access_log"] is False


def test_run_creates_the_secret_before_serving(platform_root, monkeypatch):
    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            pass

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)
    daemon.main(["run"])
    assert cfg.read_secret(cfg.load())


def test_run_warns_when_mirror_absent(platform_root, capsys, monkeypatch):
    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            pass

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)
    daemon.main(["run"])
    assert "mirror not cloned yet" in capsys.readouterr().out


def test_startup_notices_are_flushed(platform_root, monkeypatch):
    """Under nohup/systemd stdout is block-buffered; uvicorn's stderr is not.

    Without an explicit flush the operator sees uvicorn's banner but neither the
    bind notice nor how to authenticate.
    """
    flushes = []
    real_print = print

    def tracking_print(*args, **kwargs):
        if kwargs.get("flush"):
            flushes.append(args[0] if args else "")
        return real_print(*args, **kwargs)

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            pass

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)
    monkeypatch.setattr("builtins.print", tracking_print)
    daemon.main(["run"])

    assert any("Platform listening" in str(msg) for msg in flushes)
    assert any("Authenticate" in str(msg) for msg in flushes)


def test_run_loopback_notice_mentions_proxy(platform_root, capsys, monkeypatch):
    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            pass

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)
    daemon.main(["run"])
    assert "loopback only" in capsys.readouterr().out


# --- tracked-run index verbs (D25) ------------------------------------------

def test_runs_on_a_fresh_orchestrator_explains_the_scope(platform_root, capsys):
    """The empty view must say why it is empty, not just show nothing."""
    assert daemon.main(["runs"]) == 0
    out = capsys.readouterr().out

    assert "No runs tracked" in out
    assert "never the whole shared work repo" in out
    assert "lmer platform adopt" in out


def test_adopt_then_runs_then_forget(platform_root, capsys):
    assert daemon.main(["adopt", "gitlab.example.com/agents/global/develop-issue-141"]) == 0
    # The key, which is what was typed and what the index holds. Not
    # `…/runs/<slug>`: these verbs touch no directory, and for a named run that
    # path is one nobody can open (T96).
    assert "tracking gitlab.example.com/agents/global/develop-issue-141" in \
        capsys.readouterr().out

    daemon.main(["runs"])
    out = capsys.readouterr().out
    assert "1 tracked run(s)" in out
    assert "adopted" in out
    assert "gitlab.example.com/agents/global/develop-issue-141" in out
    assert "runs/develop-issue-141" not in out, (
        "the tracked listing quotes a directory it did not find; the listing that "
        "names directories is `runs --candidates`"
    )

    assert daemon.main(["forget", "gitlab.example.com/agents/global/develop-issue-141"]) == 0
    assert "no longer tracking" in capsys.readouterr().out


def test_adopt_warns_when_the_run_is_not_in_the_mirror(platform_root, capsys):
    daemon.main(["adopt", "gitlab.example.com/agents/global/nowhere"])
    assert "no such run dir is in the mirror" in capsys.readouterr().err


def test_forget_untracked_exits_nonzero(platform_root, capsys):
    assert daemon.main(["forget", "gitlab.example.com/agents/global/nope"]) == 1
    assert "not tracked" in capsys.readouterr().err


@pytest.mark.parametrize("raw,expected", [
    ("gitlab.example.com/agents/global/develop-1",
     ("gitlab.example.com", "agents/global", "develop-1")),
    ("gitlab.example.com/group/subgroup/project/review-mr-1",
     ("gitlab.example.com", "group/subgroup/project", "review-mr-1")),
    ("/gitlab.example.com/a/b/", ("gitlab.example.com", "a", "b")),
])
def test_run_ref_parsing_handles_multi_segment_projects(raw, expected):
    assert daemon._split_run_ref(raw) == expected


@pytest.mark.parametrize("raw", ["", "host", "host/project", "///"])
def test_run_ref_parsing_rejects_short_refs(raw):
    with pytest.raises(cfg.ConfigError, match="expected <host>/<project>/<slug>"):
        daemon._split_run_ref(raw)


def test_bad_run_ref_exits_two(platform_root, capsys):
    assert daemon.main(["adopt", "not-a-ref"]) == 2
    assert "expected <host>/<project>/<slug>" in capsys.readouterr().err


def test_runs_candidates_lists_the_shared_repo(platform_root, capsys, monkeypatch):
    from lmer_platform import runs as run_index
    from lmer_platform.workrepo import RunDirRef

    run_index.track("gitlab.example.com", "agents/global", "mine")
    monkeypatch.setattr(daemon, "run_dirs", lambda config: [
        RunDirRef("gitlab.example.com", "agents/global", "mine", platform_root),
        RunDirRef("gitlab.example.com", "grizz/home", "theirs", platform_root),
    ])

    daemon.main(["runs", "--candidates"])
    out = capsys.readouterr().out

    assert "these are everyone's, not the fleet view" in out
    assert "tracked] gitlab.example.com/agents/global/runs/mine" in out
    assert "-] gitlab.example.com/grizz/home/runs/theirs" in out


def test_runs_candidates_empty_mirror_is_explained(platform_root, capsys):
    daemon.main(["runs", "--candidates"])
    assert "no runs found in the work-repo mirror" in capsys.readouterr().out


# --- spawn verb (T8) --------------------------------------------------------

def _fake_result(**overrides):
    from lmer_platform.spawn import SpawnResult

    payload = {
        "session_id": "s-1", "pid": 4242, "log_path": "/logs/s-1.log",
        "host": "gitlab.example.com", "project": "agents/global", "slug": "develop-1",
        "command": ["lmer", "develop", "t"],
    }
    payload.update(overrides)
    return SpawnResult(**payload)


def test_spawn_verb_reports_the_session(platform_root, capsys, monkeypatch):
    monkeypatch.setattr(daemon, "spawn_session", lambda c, r: _fake_result())
    assert daemon.main(["spawn", "develop", "https://example.com/x"]) == 0

    out = capsys.readouterr().out
    assert "spawned s-1 (pid 4242)" in out
    assert "gitlab.example.com/agents/global/runs/develop-1" in out
    assert "/logs/s-1.log" in out


def test_spawn_verb_passes_flags_through(platform_root, monkeypatch):
    seen = {}

    def capture(config, request):
        seen["request"] = request
        return _fake_result()

    monkeypatch.setattr(daemon, "spawn_session", capture)
    daemon.main([
        "spawn", "review", "https://example.com/mr/1",
        "--repo", "https://gitlab.example.com/a/b.git", "--preset", "sol-review",
        "--harness", "codex", "--ports", "3",
    ])
    request = seen["request"]

    assert (request.taskdef, request.target) == ("review", "https://example.com/mr/1")
    assert request.repo_url == "https://gitlab.example.com/a/b.git"
    assert (request.preset, request.harness, request.ports) == ("sol-review", "codex", 3)


def test_spawn_verb_warns_when_identity_is_unknown(platform_root, capsys, monkeypatch):
    monkeypatch.setattr(
        daemon, "spawn_session",
        lambda c, r: _fake_result(host=None, project=None, slug=None),
    )
    daemon.main(["spawn", "develop", "t"])
    assert "identity unknown" in capsys.readouterr().out


#: The identity a spawn can really come back with when it has none: no host and
#: no project, or half of one from a URL like `git@host:` — and a slug either way,
#: because ``run_state.derive_slug`` answers for any taskdef.
NO_IDENTITY = [
    pytest.param(None, None, id="nothing parsed"),
    pytest.param("gitlab.example.com", "", id="half an identity"),
]


@pytest.mark.parametrize("host,project", NO_IDENTITY)
def test_spawn_verb_reports_a_run_it_could_not_identify(
    platform_root, capsys, monkeypatch, host, project
):
    """The shape an unidentified spawn actually has, which is why this regressed.

    ``slug`` is always set, so testing it printed ``run  : None/None/runs/…`` for
    a run that was never tracked and left the "identity unknown" branch
    unreachable — leaving the daemon's log as the only explanation for a row that
    vanished from the fleet view on a clean exit. The wording of *why* belongs to
    the spawn (``spawn._untracked_run_warning``, echoed by the API too), so it is
    passed through rather than restated here.
    """
    warning = (
        "this run has no identity (no repository URL was given, LMER_REPO_URL is "
        "not set, and the target 'feature/x' is not a merge request, pull request "
        "or issue URL to read one out of), so it is not tracked at all: the "
        "session appears in the fleet view only while it is alive, and its row "
        "disappears when it exits."
    )
    monkeypatch.setattr(
        daemon, "spawn_session",
        lambda c, r: _fake_result(host=host, project=project, warning=warning),
    )

    assert daemon.main(["spawn", "develop", "feature/x"]) == 0
    out = capsys.readouterr().out

    assert "runs/develop-1" not in out, (
        "a run with no identity was reported as a run directory"
    )
    assert "None" not in out
    assert "identity unknown" in out
    assert warning in out, (
        "the only place a shell user is told the run was not tracked"
    )


def test_spawn_verb_exit_codes_distinguish_capacity_from_error(platform_root,
                                                              capsys, monkeypatch):
    from lmer_platform.spawn import CapacityError, SpawnError

    monkeypatch.setattr(
        daemon, "spawn_session",
        lambda c, r: (_ for _ in ()).throw(CapacityError("cap reached: 4/4")),
    )
    assert daemon.main(["spawn", "develop", "t"]) == 1
    assert "cap reached" in capsys.readouterr().err

    monkeypatch.setattr(
        daemon, "spawn_session",
        lambda c, r: (_ for _ in ()).throw(SpawnError("cannot find lmer")),
    )
    assert daemon.main(["spawn", "develop", "t"]) == 2


def test_spawn_verb_treats_a_held_run_like_capacity(platform_root, capsys,
                                                    monkeypatch):
    """A held run is the retry-later case (exit 1), not the bad-request case.

    ``RunAlreadyLive`` subclasses ``SpawnError``, so without its own clause it
    would fall into the exit-2 branch and tell the operator to change a request
    that is fine as written.
    """
    from lmer_platform.spawn import RunAlreadyLive

    monkeypatch.setattr(
        daemon, "spawn_session",
        lambda c, r: (_ for _ in ()).throw(RunAlreadyLive("run is held")),
    )
    assert daemon.main(["spawn", "develop", "t"]) == 1
    assert "run is held" in capsys.readouterr().err


# --- lmer CLI dispatch ------------------------------------------------------

def test_lmer_cli_dispatches_platform_subcommand(monkeypatch):
    """`lmer platform ...` must route before task parsing, like `lmer build`."""
    from lmer_cli import cli

    seen = {}

    def fake_platform_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(daemon, "main", fake_platform_main)
    assert cli.main(["platform", "status", "--json"]) == 0
    assert seen["argv"] == ["status", "--json"]


def test_platform_dispatch_is_lazy():
    """Importing lmer_cli.cli must not drag in FastAPI for every invocation."""
    import ast
    import inspect

    from lmer_cli import cli

    source = inspect.getsource(cli)
    tree = ast.parse(source)
    top_level_imports = [
        alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    ]
    module_names = [
        node.module for node in tree.body if isinstance(node, ast.ImportFrom)
    ]
    assert not any("lmer_platform" in (n or "") for n in top_level_imports)
    assert not any("lmer_platform" in (n or "") for n in module_names)


# --- .env loading -----------------------------------------------------------
#
# `lmer platform` dispatches before lmer's own argument parsing, so without an
# explicit load it never sees ~/.lmer/.env — where LMER_WORK_REPO and its token
# live. The daemon then starts with no work repo and an empty fleet view, which
# looks like a bug rather than a missing export.

def test_state_dir_env_file_is_loaded(tmp_path, monkeypatch):
    from lmer_cli import runtime

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".env").write_text(
        "LMER_WORK_REPO=https://git.example.com/agents/work.git\n", encoding="utf-8"
    )
    monkeypatch.setattr(runtime, "_LMER_STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(runtime, "lmer_state_dir", lambda: state_dir)
    monkeypatch.delenv("LMER_WORK_REPO", raising=False)
    monkeypatch.chdir(tmp_path)

    daemon._load_env_files()
    assert os.environ["LMER_WORK_REPO"] == "https://git.example.com/agents/work.git"


def test_exported_variable_beats_the_env_file(tmp_path, monkeypatch):
    from lmer_cli import runtime

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".env").write_text("LMER_WORK_REPO=from-file\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "lmer_state_dir", lambda: state_dir)
    monkeypatch.setenv("LMER_WORK_REPO", "from-export")
    monkeypatch.chdir(tmp_path)

    daemon._load_env_files()
    assert os.environ["LMER_WORK_REPO"] == "from-export"


def test_working_directory_env_file_outranks_the_state_dir(tmp_path, monkeypatch):
    from lmer_cli import runtime

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".env").write_text("LMER_PLATFORM_BIND_PORT=9001\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("LMER_PLATFORM_BIND_PORT=9002\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "lmer_state_dir", lambda: state_dir)
    monkeypatch.delenv("LMER_PLATFORM_BIND_PORT", raising=False)
    monkeypatch.chdir(project)

    daemon._load_env_files()
    assert os.environ["LMER_PLATFORM_BIND_PORT"] == "9002"


def test_missing_env_files_are_not_fatal(tmp_path, monkeypatch):
    from lmer_cli import runtime

    monkeypatch.setattr(runtime, "lmer_state_dir", lambda: tmp_path / "absent")
    monkeypatch.chdir(tmp_path)
    daemon._load_env_files()  # must not raise


def test_main_loads_env_files_before_dispatch(tmp_path, monkeypatch, platform_root):
    """The load has to happen before any command resolves configuration."""
    from lmer_cli import runtime

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".env").write_text(
        "LMER_PLATFORM_BIND_PORT=9123\n", encoding="utf-8"
    )
    monkeypatch.setattr(runtime, "lmer_state_dir", lambda: state_dir)
    monkeypatch.delenv("LMER_PLATFORM_BIND_PORT", raising=False)
    monkeypatch.chdir(tmp_path)

    seen = {}
    monkeypatch.setitem(
        daemon._COMMANDS, "status",
        lambda args: seen.setdefault("port", cfg.load().bind_port) and 0 or 0,
    )
    daemon.main(["status"])
    assert seen["port"] == 9123
