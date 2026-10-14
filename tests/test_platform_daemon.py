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
    assert daemon.main(["--workspace-id", "general", "adopt", "gitlab.example.com/agents/global/develop-issue-141"]) == 0
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
    daemon.main(["--workspace-id", "general", "adopt", "gitlab.example.com/agents/global/nowhere"])
    assert "no such run dir is in the mirror" in capsys.readouterr().err


def test_forget_untracked_exits_nonzero(platform_root, capsys):
    assert daemon.main(["forget", "gitlab.example.com/agents/global/nope"]) == 1
    assert "not tracked" in capsys.readouterr().err


# --- by uid (spec §6.1): a 32-hex last segment is a schema-2 run's uid -------

UID = "f" * 32
UID_KEY = f"gitlab.example.com/acme/widgets/{UID}"


def _plant_uid_record(uid=UID, slug="chat", launcher=None):
    import yaml

    config = cfg.load()
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        yaml.safe_dump({
            "schema": 2, "name": "example-plexus",
            "projects": {"widgets": {"repo": "ssh://git@gitlab.example.com/acme/widgets.git"}},
        }),
        encoding="utf-8",
    )
    path = config.mirror_path / "widgets" / "_runs" / slug / uid
    path.mkdir(parents=True, exist_ok=True)
    session = {"started_at": "2026-01-01T00:00:00+00:00"}
    if launcher is not None:
        session["launcher"] = launcher
    (path / "state.yaml").write_text(
        yaml.safe_dump({"schema": 2, "slug": slug, "status": "in-progress",
                        "session": session}),
        encoding="utf-8",
    )


def test_adopt_by_uid_tracks_the_record_under_its_uid(platform_root, capsys):
    from lmer_platform import runs

    _plant_uid_record()

    assert daemon.main(["--workspace-id", "general", "adopt", UID_KEY]) == 0

    captured = capsys.readouterr()
    assert f"tracking {UID_KEY}" in captured.out
    assert "no such run dir" not in captured.err
    entry = runs.get_tracked("gitlab.example.com", "acme/widgets", uid=UID)
    assert entry is not None and entry.slug == "chat", "the label is the record's slug"


def test_adopt_by_uid_binds_the_slug_entry_the_transition_left_unresolved(
    platform_root, capsys
):
    """The daemon CLI never builds state, so the entry has not been examined
    when the adopt runs: the adopt examines it first. Two records and no
    launcher evidence leave it unresolved with both as candidates, and the uid
    named is one of them."""
    from lmer_platform import runs

    _plant_uid_record()
    _plant_uid_record("0" * 32)
    runs.track("gitlab.example.com", "acme/widgets", "chat")

    assert daemon.main(["--workspace-id", "general", "adopt", UID_KEY]) == 0

    index = runs.load_index()
    assert set(index) == {UID_KEY}
    assert index[UID_KEY]["aliases"] == ["gitlab.example.com/acme/widgets/chat"]


def test_adopt_by_uid_never_moves_another_launchs_state_onto_it(platform_root, capsys):
    """Review !387 round 3, discussion 8. The slug entry's last session is
    launch Y by the launcher rule, so the transition binds it to Y; adopting X
    then tracks X as a row of its own. Binding the unexamined entry to X would
    have carried Y's title onto X and dropped Y's row."""
    import socket

    from lmer_platform import meta, registry, runs

    host, project = "gitlab.example.com", "acme/widgets"
    other = "0" * 32
    other_key = f"{host}/{project}/{other}"
    _plant_uid_record()
    _plant_uid_record(other, launcher={
        "pid": 4242, "hostname": socket.gethostname(),
        "started_at": "2026-02-01T00:00:05+00:00",
    })
    registry.register("s-launch-y", pid=4242, started_at="2026-02-01T00:00:00Z",
                      run={"host": host, "project": project, "slug": "chat"})
    runs.track(host, project, "chat", session_id="s-launch-y")
    meta.write(host, project, "chat", title="launch Y")

    assert daemon.main(["--workspace-id", "general", "adopt", UID_KEY]) == 0

    index = runs.load_index()
    assert set(index) == {UID_KEY, other_key}
    assert index[other_key]["aliases"] == [f"{host}/{project}/chat"]
    assert index[UID_KEY]["source"] == "adopted"
    assert meta.read(host, project, other).title == "launch Y"
    assert meta.read(host, project, UID).title == ""


def test_adopt_by_a_uid_the_mirror_does_not_hold_exits_two(platform_root, capsys):
    from lmer_platform import runs

    assert daemon.main(["--workspace-id", "general", "adopt", UID_KEY]) == 2
    assert UID in capsys.readouterr().err
    assert runs.load_index() == {}


def test_forget_by_uid_forgets_that_launch(platform_root, capsys):
    from lmer_platform import runs

    runs.track("gitlab.example.com", "acme/widgets", "chat", uid=UID)
    runs.track("gitlab.example.com", "acme/widgets", "chat", uid="0" * 32)

    assert daemon.main(["forget", UID_KEY]) == 0

    assert f"no longer tracking {UID_KEY}" in capsys.readouterr().out
    assert set(runs.load_index()) == {f"gitlab.example.com/acme/widgets/{'0' * 32}"}


def test_forget_by_the_slug_a_bind_left_forgets_the_bound_launch(platform_root, capsys):
    """Review !387 round 3, discussion 10: the old slug key is an alias of the
    bound entry, and ``lmer-ctl runs forget`` and the UI follow it."""
    from lmer_platform import run_transition, runs

    _plant_uid_record()
    runs.track("gitlab.example.com", "acme/widgets", "chat")
    run_transition.bind(cfg.load(), "gitlab.example.com", "acme/widgets", "chat", UID)
    assert set(runs.load_index()) == {UID_KEY}

    assert daemon.main(["forget", "gitlab.example.com/acme/widgets/chat"]) == 0

    assert f"no longer tracking {UID_KEY}" in capsys.readouterr().out
    assert runs.load_index() == {}


def test_resume_by_uid_addresses_the_launch(platform_root, monkeypatch, capsys):
    from lmer_platform import resume

    seen = []

    def refuse(config, request):
        seen.append(request)
        raise resume.ResumeError("stop here")

    monkeypatch.setattr(daemon, "resume_run", refuse)

    daemon.main(["--workspace-id", "general", "resume", UID_KEY])

    (request,) = seen
    assert request.uid == UID and not request.slug


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
        RunDirRef("gitlab.example.com", "colleague/home", "theirs", platform_root),
    ])

    daemon.main(["runs", "--candidates"])
    out = capsys.readouterr().out

    assert "these are everyone's, not the fleet view" in out
    assert "tracked] gitlab.example.com/agents/global/runs/mine" in out
    assert "-] gitlab.example.com/colleague/home/runs/theirs" in out


# --- the CLI twin of /api/runs/candidates folds identities the same way ------
#
# Two surfaces answering "is this directory tracked" have to answer it the same
# way, or the operator gets one answer from `lmer platform runs --candidates`
# and the other from the HTTP route about the same directory — and acting on
# the CLI's answer writes the second index row the fold exists to prevent
# (!269 review round 2).

CANDIDATE_PRIMARY = "gitlab.example.com/acme/widgets"
CANDIDATE_SECONDARY = "github.com/acme/widgets"


def _register_widgets_for_daemon(config):
    """A mirror whose manifest knows ``widgets`` at both of its remotes (D8)."""
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        "schema: 1\nname: example-plexus\nprojects:\n  widgets:\n"
        f"    repo: {CANDIDATE_PRIMARY}\n    remotes: [{CANDIDATE_SECONDARY}]\n",
        encoding="utf-8",
    )


def test_runs_candidates_folds_both_sides_like_the_http_route(
    platform_root, capsys, monkeypatch
):
    """A row tracked under the project's SECONDARY remote, and the walk keying
    the same directory on the PRIMARY: raw pairs never match, so this listing
    offered for adoption a run the platform already had."""
    from lmer_platform import runs as run_index
    from lmer_platform.config import load as load_config
    from lmer_platform.workrepo import RunDirRef

    config = load_config()
    _register_widgets_for_daemon(config)
    secondary_host, secondary_project = CANDIDATE_SECONDARY.split("/", 1)
    run_index.track(secondary_host, secondary_project, "review-mr-1",
                    source="adopted")
    primary_host, primary_project = CANDIDATE_PRIMARY.split("/", 1)
    monkeypatch.setattr(daemon, "run_dirs", lambda config: [
        RunDirRef(primary_host, primary_project, "review-mr-1", platform_root),
    ])

    daemon.main(["runs", "--candidates"])
    out = capsys.readouterr().out

    assert "[tracked]" in out, (
        "the CLI listing offered for adoption a directory already tracked "
        f"under one of its own remotes; the HTTP route calls it tracked:\n{out}"
    )


def test_runs_candidates_still_offers_a_genuinely_untracked_run(
    platform_root, capsys, monkeypatch
):
    """The converse, so folding both sides here is not a widening."""
    from lmer_platform.config import load as load_config
    from lmer_platform.workrepo import RunDirRef

    config = load_config()
    _register_widgets_for_daemon(config)
    primary_host, primary_project = CANDIDATE_PRIMARY.split("/", 1)
    monkeypatch.setattr(daemon, "run_dirs", lambda config: [
        RunDirRef(primary_host, primary_project, "review-mr-2", platform_root),
    ])

    daemon.main(["runs", "--candidates"])
    out = capsys.readouterr().out

    assert "[      -]" in out


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


# --- resume verb ------------------------------------------------------------

RESUME_RUN = "gitlab.example.com/agents/global/develop-141"


def _fake_resume_result(**overrides):
    from lmer_platform.resume import ResumeResult

    payload = {
        "host": "gitlab.example.com", "project": "agents/global",
        "slug": "develop-141", "taskdef": "develop", "target": "issue-141",
        "continued": True, "started_slug": "develop-141",
        "session": _fake_result(),
        "rel_path": "agents/global/_runs/develop-141/20260824-165230-2-31245",
    }
    payload.update(overrides)
    return ResumeResult(**payload)


def test_resume_verb_reports_the_run_it_continued(platform_root, capsys,
                                                  monkeypatch):
    monkeypatch.setattr(daemon, "resume_run", lambda c, r: _fake_resume_result())
    assert daemon.main(["resume", RESUME_RUN]) == 0

    out = capsys.readouterr().out
    assert "resumed gitlab.example.com/agents/global/runs/develop-141" in out
    assert "session : s-1 (pid 4242)" in out


#: The two spellings of one launch's address: the id the container minted, and
#: the ``<session-id>--<name>`` the naming rename produced. Both are what a fleet
#: row can show, so both have to survive the terminal unchanged — the resolution
#: is ``resume._require_session``'s job, not this parser's.
SESSION_SPELLINGS = [
    pytest.param("20260824-165230-2-31245", id="bare id"),
    pytest.param("20260824-165230-2-31245--auth-rate-limit", id="named record"),
]


@pytest.mark.parametrize("session_id", SESSION_SPELLINGS)
def test_resume_verb_passes_the_named_launch_through(platform_root, monkeypatch,
                                                     session_id):
    """The flag has to reach ``ResumeRequest.session_id``, not merely parse.

    A ``--session`` that argparse accepts and nobody reads is the worst shape
    this can take: the resume continues whichever launch the run resolves to —
    the newest — and reports success about a record the operator did not name.
    """
    seen = {}

    def capture(config, request):
        seen["request"] = request
        return _fake_resume_result()

    monkeypatch.setattr(daemon, "resume_run", capture)
    daemon.main([
        "resume", RESUME_RUN, "--session", session_id,
        "--prompt", "finish the migration",
        "--repo", "https://gitlab.example.com/agents/global.git",
    ])
    request = seen["request"]

    assert request.session_id == session_id
    assert (request.host, request.project, request.slug) == (
        "gitlab.example.com", "agents/global", "develop-141"
    )
    assert request.direction == "finish the migration"
    assert request.repo_url == "https://gitlab.example.com/agents/global.git"


def test_resume_without_a_session_continues_the_newest_launch(platform_root,
                                                              monkeypatch):
    """Omitted is ``None``, which is what ``resume_run`` reads as "the record the
    run resolves to" — the one-tap continue, and the only behaviour a terminal
    had before the flag existed."""
    seen = {}

    def capture(config, request):
        seen["request"] = request
        return _fake_resume_result()

    monkeypatch.setattr(daemon, "resume_run", capture)
    daemon.main(["resume", RESUME_RUN])

    assert seen["request"].session_id is None


def test_resume_verb_refuses_an_unresolvable_session_with_its_list(
    platform_root, capsys, monkeypatch
):
    """The refusal an operator actually hits: an id that names no launch here.

    Exit 2 because the remedy is a different id, and the message is passed
    through rather than restated — ``resume.SessionNotFound`` is the only thing
    that knows which launches the group holds.
    """
    from lmer_platform.resume import SessionNotFound

    refusal = SessionNotFound(
        "agents/global/_runs/develop-141 holds no session 'nope' — this run's "
        "launches are: 20260824-165230-2-31245"
    )
    monkeypatch.setattr(
        daemon, "resume_run",
        lambda c, r: (_ for _ in ()).throw(refusal),
    )
    assert daemon.main(["resume", RESUME_RUN, "--session", "nope"]) == 2
    assert "launches are: 20260824-165230-2-31245" in capsys.readouterr().err


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
