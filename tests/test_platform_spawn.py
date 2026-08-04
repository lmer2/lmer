"""Tests for spawning platform-owned sessions (issue #141, slice M1 / T8).

Sessions are spawned for real — against a stub script rather than the actual
``lmer``, so the PTY, the drain thread, the log tee and the exit handling are all
genuinely exercised without launching a container.

The properties that matter: the log outlives the session (it is the scrollback
source), a clean exit clears the registry entry while a crash deliberately leaves
it (that entry is the crash signal), the run is tracked so it appears in this
orchestrator's own view, every session comes up with its control plane exposed and
its token on disk but never in the entry (spec D8 / §6.2), and the concurrency cap
actually holds over the sessions it is about — workers, since the orchestrating
assistant holds its own slot (T75).
"""

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from lmer_cli import supervisor
from lmer_cli.cli import parse_args, parse_dir_mount_specs
from lmer_platform import config as cfg
from lmer_platform import ask, registry, runs, session_io, spawn, store, transcripts
from tests.conftest import strip_lmer_env
from work_repo import run_state


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_REPO_URL", "LMER_PLATFORM_PORTS_FILE", "LMER_TASK",
                 "LMER_TASK_TARGET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    """A stub standing in for `lmer`: prints, then exits with a chosen code.

    ``FAKE_LMER_SESSION_LOG`` makes it stand in for a session image new enough to
    write the session's own log (#150); left unset it is an older image, which is
    the state a mixed fleet is permanently in.

    With ``FAKE_LMER_TRANSCRIPT`` set it also stands in for the *harness*, writing
    that text as a JSONL transcript. The host directory is not passed in — it is
    read out of the ``--mount-dir`` argument the stub was launched with, and the
    subdirectory mirrors the per-workspace level the harness keeps. So a mount
    argument that is malformed, points at a directory that does not exist, or
    points somewhere the platform will not look for it fails the test that asserts
    on the transcript, rather than being asserted about in a comment.

    A spawn passes more than one ``--mount-dir`` (the transcript and the ask
    channel, T23), so the stub picks the mount by its *container destination*
    rather than taking whichever came last — the same way the harness inside
    would find its projects directory.
    """
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        'if [ -n "$FAKE_LMER_PORTS_JSON" ] && [ -n "$LMER_PLATFORM_PORTS_FILE" ]; then\n'
        '  printf "%s" "$FAKE_LMER_PORTS_JSON" > "$LMER_PLATFORM_PORTS_FILE"\n'
        "fi\n"
        'if [ -n "$FAKE_LMER_TRANSCRIPT" ]; then\n'
        '  spec=""; prev=""\n'
        '  for arg in "$@"; do\n'
        '    if [ "$prev" = "--mount-dir" ]; then\n'
        '      case "$arg" in\n'
        f'        *:{transcripts.CONTAINER_TRANSCRIPT_DIR}:*) spec="$arg" ;;\n'
        "      esac\n"
        "    fi\n"
        '    prev="$arg"\n'
        "  done\n"
        '  host_dir="${spec%%:*}"\n'
        '  if [ -d "$host_dir" ]; then\n'
        '    mkdir -p "$host_dir/-workspace"\n'
        '    printf "%s" "$FAKE_LMER_TRANSCRIPT" > "$host_dir/-workspace/session.jsonl"\n'
        "  fi\n"
        "fi\n"
        # The ask channel's two halves as the child actually sees them: it finds
        # the mount by destination and records the env var's value through it, so
        # a mount without the variable (or the other way round) fails a test
        # rather than being asserted about in a comment.
        'ask_spec=""; prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "--mount-dir" ]; then\n'
        "    case \"$arg\" in\n"
        f'      *:{ask.CONTAINER_ASK_DIR}:*) ask_spec="$arg" ;;\n'
        "    esac\n"
        "  fi\n"
        '  prev="$arg"\n'
        "done\n"
        'ask_host="${ask_spec%%:*}"\n'
        'if [ -n "$ask_host" ] && [ -d "$ask_host" ]; then\n'
        '  printf "%s" "${LMER_ASK_DIR-<unset>}" > "$ask_host/child-saw-env"\n'
        "fi\n"
        # With FAKE_LMER_SESSION_LOG set the stub stands in for a *newer image*:
        # one whose in-container supervisor writes the session's own log into the
        # directory the platform mounted for it. Without it the stub is an older
        # image, which is the case the read path must keep handling forever — so
        # the default is deliberately the one that writes nothing.
        'if [ -n "$FAKE_LMER_SESSION_LOG" ]; then\n'
        '  log_spec=""; prev=""\n'
        '  for arg in "$@"; do\n'
        '    if [ "$prev" = "--mount-dir" ]; then\n'
        "      case \"$arg\" in\n"
        # spawn's copy of the constant, because that is what built the mount this
        # stub is matching — the suite redirects the *writer's* copy away from the
        # live session log (``_isolate_session_log_dir``).
        f'        *:{spawn.CONTAINER_SESSION_LOG_DIR}:*) log_spec="$arg" ;;\n'
        "      esac\n"
        "    fi\n"
        '    prev="$arg"\n'
        "  done\n"
        '  log_host="${log_spec%%:*}"\n'
        '  if [ -d "$log_host" ]; then\n'
        f'    printf "%s" "$FAKE_LMER_SESSION_LOG" > "$log_host/{supervisor.SESSION_LOG_NAME}"\n'
        "  fi\n"
        "fi\n"
        # Staying alive is how a test keeps the registry entry around: a clean
        # exit reaps it, which would otherwise race any assertion about it.
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
        "target": "https://gitlab.example.com/agents/global/-/work_items/141",
        "repo_url": "https://gitlab.example.com/agents/global.git",
    }
    payload.update(overrides)
    return spawn.SpawnRequest(**payload)


def wait_for(predicate, timeout=5.0):
    """Poll until *predicate* holds — the drain thread finishes asynchronously."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- request validation -----------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"taskdef": ""}, {"taskdef": "   "}, {"target": ""}, {"ports": -1},
    {"ports": "two"}, {"ports": True},
])
def test_invalid_requests_are_rejected(config, overrides):
    with pytest.raises(spawn.SpawnError):
        spawn.spawn_session(config, request_for(**overrides))


def test_invalid_kind_is_rejected(config):
    with pytest.raises(spawn.SpawnError, match="invalid session kind"):
        spawn.spawn_session(config, request_for(), kind="supervisor")


# --- executable resolution --------------------------------------------------

def test_configured_lmer_bin_wins(config, fake_lmer):
    assert spawn.resolve_lmer_bin(config) == str(fake_lmer)


def test_missing_lmer_is_a_clear_error(platform_root, monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda _name: None)
    with pytest.raises(spawn.SpawnError, match="cannot find the `lmer` executable"):
        spawn.resolve_lmer_bin(cfg.load())


def test_unstartable_command_reports_cleanly(platform_root, tmp_path):
    config = cfg.load({"lmer_bin": str(tmp_path / "does-not-exist")})
    with pytest.raises(spawn.SpawnError, match="cannot start"):
        spawn.spawn_session(config, request_for())


# --- run identity -----------------------------------------------------------

def test_run_identity_is_predicted_before_the_container_writes_anything(config):
    host, project, slug = spawn.derive_run_identity(
        request_for(), "https://gitlab.example.com/agents/global.git"
    )
    assert (host, project) == ("gitlab.example.com", "agents/global")
    assert slug.startswith("develop-")


def test_run_identity_tolerates_an_unparseable_repo(config):
    host, project, slug = spawn.derive_run_identity(request_for(), "")
    assert (host, project) == (None, None)
    assert slug


def test_ssh_repo_urls_resolve(config):
    host, project, _ = spawn.derive_run_identity(
        request_for(), "git@gitlab.example.com:agents/global.git"
    )
    assert (host, project) == ("gitlab.example.com", "agents/global")


# --- spawning ---------------------------------------------------------------

def test_spawn_registers_tracks_and_logs(config, monkeypatch):
    # The stub must outlive the assertion. `registry.read_session` reads the entry
    # the watcher REAPS on a clean exit, so without this the stub exits first under
    # load and the entry is legitimately gone — green on an idle machine, red in a
    # gate run. `runs.get_tracked` below needs no such help: the tracked index is
    # durable and survives the reap, which is why only half of this test raced.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())

    entry = registry.read_session(result.session_id)
    assert entry is not None
    assert entry["pid"] == result.pid
    assert entry["task"]["taskdef"] == "develop"
    assert entry["run"]["host"] == "gitlab.example.com"
    assert entry["log_path"] == str(result.log_path)

    try:
        tracked = runs.get_tracked("gitlab.example.com", "agents/global", result.slug)
        assert tracked is not None, (
            "a spawned run must appear in this orchestrator's view"
        )
        assert tracked.source == "spawned"
        assert tracked.last_session_id == result.session_id
    finally:
        os.kill(result.pid, 9)


def test_spawn_writes_the_pty_log(config):
    result = spawn.spawn_session(config, request_for())
    assert wait_for(lambda: result.log_path.is_file() and result.log_path.stat().st_size)
    assert "fake lmer started" in result.log_path.read_text(encoding="utf-8")


def test_log_outlives_the_session(config):
    """The log is the scrollback source (D16), so it must survive the exit."""
    result = spawn.spawn_session(config, request_for())
    assert wait_for(lambda: registry.read_session(result.session_id) is None)
    # Waited for, not asserted outright, because the reap and the tee are
    # different threads: `_watch` removes the entry the moment `Popen.wait`
    # returns, while `_drain` may still be between its `open` and its first
    # write. That lag is the code's documented behaviour, not a defect — it is
    # exactly why `follow_log` drains once more after a grace tick — so a bare
    # assertion here tests thread scheduling rather than the property. On a
    # loaded runner it loses: CI pipeline 1448 caught the file present with
    # `st_size=0`, while `test_spawn_writes_the_pty_log` above, which waits,
    # passed 20ms earlier in the same run.
    assert wait_for(
        lambda: result.log_path.is_file() and result.log_path.stat().st_size > 0
    )


def test_command_always_exposes_the_control_plane(config):
    """No request can turn --fastapi off: an unwritable session is uncontrollable.

    Two *different* targets, because one run may only have one session: the same
    taskdef and target twice derives one run identity, and the second spawn is
    refused for that reason rather than for anything to do with this flag.
    """
    plain = spawn.spawn_session(config, request_for())
    assert "--fastapi" in plain.command

    with_extras = spawn.spawn_session(config, request_for(
        target="https://gitlab.example.com/agents/global/-/work_items/142",
        extra_args=("--verbose",),
    ))
    assert "--fastapi" in with_extras.command


def test_command_includes_optional_flags(config):
    result = spawn.spawn_session(
        config, request_for(preset="sol-review", harness="codex", ports=2)
    )
    assert result.command[1:3] == ["develop", request_for().target]
    assert "--preset" in result.command and "sol-review" in result.command
    assert "--harness" in result.command and "codex" in result.command
    assert "--ports" in result.command and "2" in result.command


def test_extra_args_are_appended(config):
    result = spawn.spawn_session(config, request_for(extra_args=("--verbose",)))
    assert result.command[-1] == "--verbose"


def test_repo_url_falls_back_to_environment(config, monkeypatch):
    monkeypatch.setenv("LMER_REPO_URL", "https://gitlab.example.com/agents/other.git")
    result = spawn.spawn_session(config, request_for(repo_url=None))
    assert result.project == "agents/other"


# --- the run's repo of record vs. what its identity is derived from ----------
#
# Two claims, and only one of them is written down. The recorded URL lands in the
# registry entry and in the tracked index, where `resume` then believes it — which
# is why it refuses to invent one. An identity URL only has to parse back to the
# run's own host and project, which is all `derive_run_identity` needs, so a
# caller that can reconstruct that much (answer.py, for an adopted run) can join a
# session to its run without leaving a URL nobody supplied behind.

IDENTITY_URL = "https://gitlab.example.com/agents/global"


def test_an_identity_url_is_derived_from_but_never_recorded(config):
    result = spawn.spawn_session(
        config, request_for(repo_url=None, identity_repo_url=IDENTITY_URL)
    )
    assert (result.host, result.project) == ("gitlab.example.com", "agents/global")

    tracked = runs.get_tracked(result.host, result.project, result.slug)
    assert tracked is not None, "the session must still be joined to its run"
    assert tracked.repo is None, (
        "a URL the caller could only reconstruct must not become the run's repo of "
        "record — every later verb believes that field, and resume asks for it "
        "rather than guessing"
    )


def test_the_entry_of_a_session_with_only_an_identity_url_records_no_repo(
    config, monkeypatch
):
    """The index is not the only place a spawn writes the URL it was handed."""
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(
        config, request_for(repo_url=None, identity_repo_url=IDENTITY_URL)
    )
    try:
        assert registry.read_session(result.session_id)["task"]["repo"] is None
    finally:
        os.kill(result.pid, 9)


def test_a_recorded_repo_url_wins_over_an_identity_url(config):
    """Given both, the evidence beats the reconstruction — for identity too."""
    result = spawn.spawn_session(config, request_for(
        repo_url="https://gitlab.example.com/agents/global.git",
        identity_repo_url="https://gitlab.example.com/agents/elsewhere",
    ))
    assert result.project == "agents/global"
    assert runs.get_tracked(result.host, result.project, result.slug).repo == (
        "https://gitlab.example.com/agents/global.git"
    )


def test_an_identity_url_stops_the_environment_fallback(config, monkeypatch):
    """A caller that reconstructed the identity is not asking for the daemon's.

    The fallback would otherwise derive the run from a URL that has nothing to do
    with it *and* record that URL as its repo — the same invented-URL problem,
    sourced from the daemon's shell instead of from a reconstruction.
    """
    monkeypatch.setenv("LMER_REPO_URL", "https://gitlab.example.com/agents/other.git")
    result = spawn.spawn_session(
        config, request_for(repo_url=None, identity_repo_url=IDENTITY_URL)
    )
    assert result.project == "agents/global"
    assert runs.get_tracked(result.host, result.project, result.slug).repo is None


def test_a_no_repo_request_does_not_fall_back_to_the_environment(config, monkeypatch):
    """"No URL supplied" and "no repository" are different requests (spec D17).

    The assistant sets the second one deliberately; with only an absent repo_url
    to go on, the fallback fires whenever the daemon's shell exported
    LMER_REPO_URL and the session is filed under a repository it has no checkout
    of.
    """
    monkeypatch.setenv("LMER_REPO_URL", "https://gitlab.example.com/agents/other.git")
    result = spawn.spawn_session(config, request_for(repo_url=None, no_repo=True))

    assert (result.host, result.project) == (None, None)
    assert runs.list_tracked() == [], (
        "a session with no repository has no run to file under one"
    )


@pytest.mark.parametrize("overrides", [
    {"repo_url": "https://gitlab.example.com/agents/global.git"},
    {"repo_url": None, "identity_repo_url": IDENTITY_URL},
])
def test_no_repo_together_with_a_repo_url_is_refused(config, overrides):
    """Incoherent rather than a precedence question, so it is said out loud."""
    with pytest.raises(spawn.SpawnError, match="no_repo is set together with"):
        spawn.spawn_session(config, request_for(no_repo=True, **overrides))
    assert registry.list_sessions(live_only=False) == []


def test_session_without_run_identity_is_still_registered(config, caplog, monkeypatch):
    # The stub has to outlive the assertion. Without this it exits immediately, the
    # watcher thread reaps the entry, and `read_session` races it — green on an idle
    # machine and red under load, which is exactly how it failed in a gate run. The
    # stub's own comment names this; see FAKE_LMER_SLEEP where it is defined.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for(repo_url="not-a-url"))

    try:
        assert registry.read_session(result.session_id) is not None
        assert runs.list_tracked() == [], "an unidentifiable run must not be invented"
        assert any("platform_spawn_untracked_run" in r.message for r in caplog.records)
    finally:
        os.kill(result.pid, 9)


# --- the identity in the target (T28) ----------------------------------------
#
# `lmer develop <MR-url>` needs no repo flag because the repository is nearly
# always *in* the target, and the run dialog leaves the field blank as a matter of
# course. A spawn that gave up on the identity there was not merely missing a
# nicety: with no host/project the run is never tracked, so its only record is the
# registry entry — which a clean exit removes, taking the row out of the fleet view
# and leaving nothing behind but a log line.
#
# So the target is read as a third source of an identity, using the same helper
# `lmer` runs on it, and never as a source of a *record*: a derived URL is a
# reconstruction (and in the GitLab case a tokenised one), and recording a
# reconstruction is what answer.py's `_identity_repo_url` docstring explains at
# length would silently satisfy `resume.RepoUrlRequired` forever after.

#: A target whose project name ends in one of the characters `.git` is made of.
#: Named because the parity test below cannot see what was wrong with it: until
#: T48 the shared parser's SSH branch removed the suffix with ``rstrip(".git")``,
#: so this one's identity was the truncated `group/subgroup/projec` — on *both*
#: sides, which is precisely why parity held while the name was wrong. Not a
#: corner case either: the work repo holds `docs/lmer-doc-bot` and
#: `openpipes/openpipes.net`.
GIT_CHAR_TAILED_TARGET = (
    "https://gitlab.example.com/group/subgroup/project/-/issues/12"
)

#: Resource URLs a target realistically is, one per shape the shared helper knows.
DERIVABLE_TARGETS = [
    "https://gitlab.example.com/agents/global/-/work_items/141",
    "https://gitlab.example.com/agents/global/-/merge_requests/7",
    GIT_CHAR_TAILED_TARGET,
    "https://github.com/owner/repo/pull/9",
]

#: Targets that name no repository at all: the residual case, and the one the
#: warning exists for.
UNDERIVABLE_TARGETS = ["feature/some-branch", "make the flaky test stop", "v1.2.3"]

#: A token in the shape a host token has. Present in the daemon's environment is
#: the *normal* state for an orchestrator (it pulls the work repo with one), and
#: it is what makes the derived URL a credential.
HOST_TOKEN = "glpat-not-a-real-token-000"
TOKEN_ENV = "GITLAB_TOKEN_gitlab_example_com"


@pytest.fixture
def no_host_tokens(monkeypatch):
    """No token for any host: the derivation answers in SSH shape.

    Ambient ``GITLAB_TOKEN*`` is not stripped by ``strip_lmer_env`` (it is not an
    ``LMER_`` name), and a developer machine running this suite has one — so a
    test about the token-less spelling has to say so.
    """
    for name in list(os.environ):
        if name.startswith(("GITLAB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def host_token(monkeypatch, no_host_tokens):
    """Exactly one host token, for gitlab.example.com."""
    monkeypatch.setenv(TOKEN_ENV, HOST_TOKEN)
    return HOST_TOKEN


def lmer_would_export(target):
    """``(host, project)`` ``lmer`` itself exports for *target*, via its own chain.

    Not a re-implementation to compare against: these are the CLI's functions in
    the CLI's order — derive from the target, normalize, inject a host token,
    parse — which is how ``cli.main`` fills ``LMER_REPO_HOST`` /
    ``LMER_REPO_PROJECT`` (``cli.py``, the block just before the container env
    dict). Those two are what the container files the run directory under, so this
    is the only thing the platform's prediction can be checked against without a
    container: if the two disagree the platform tracks a row for a run directory
    that never appears.

    Deliberately compared as *parity* rather than against expected literals: the
    platform must reproduce the container's answer, not a better one. That is
    also why it survived T48, which fixed a wart in the shared parser's SSH branch
    (``rstrip(".git")`` ate trailing ``.git`` characters, so ``group/project``
    read back as ``group/projec``) — the two sides moved together, as they must.
    What parity cannot see is the name itself being wrong, which is what
    test_a_project_name_ending_in_a_git_character_is_not_truncated pins.
    """
    from lmer_cli.cli import _derive_repo_url_from_task_target, _parse_repo_url
    from lmer_cli.resolve import normalize_repo_url
    from lmer_cli.tokens import _inject_gitlab_token_if_available

    derived = _derive_repo_url_from_task_target(target)
    url, _local = normalize_repo_url(derived or target, Path.cwd(), None)
    return _parse_repo_url(_inject_gitlab_token_if_available(url))


@pytest.mark.parametrize("target", DERIVABLE_TARGETS)
def test_a_target_that_carries_the_repository_identifies_the_run(config, target):
    """The bug, from the outside: no repo URL anywhere, and the run is tracked."""
    result = spawn.spawn_session(config, request_for(repo_url=None, target=target))

    assert (result.host, result.project) != (None, None)
    tracked = runs.get_tracked(result.host, result.project, result.slug)
    assert tracked is not None, (
        "a run with no identity is never recorded, so its row disappears with the "
        "session that was carrying it"
    )
    assert tracked.source == "spawned"
    assert tracked.last_session_id == result.session_id


@pytest.mark.parametrize("target", DERIVABLE_TARGETS)
@pytest.mark.parametrize("tokens", ["with a host token", "without one"])
def test_the_derived_identity_is_the_one_lmer_will_export(
    config, monkeypatch, target, tokens, no_host_tokens
):
    """Parity with the child, which is the only thing that makes it the right answer.

    Run under both token states because the shared helper answers in a different
    *shape* for each — tokenised HTTPS when it finds a token for the host, SSH when
    it does not — and the platform has to match the container in both.
    """
    if tokens == "with a host token":
        monkeypatch.setenv(TOKEN_ENV, HOST_TOKEN)
    request = request_for(repo_url=None, target=target)

    _recorded, identity = spawn._repo_urls(request)
    host, project, _slug = spawn.derive_run_identity(request, identity)

    assert (host, project) == lmer_would_export(target), (
        "the platform predicted a run directory the container will not create"
    )


@pytest.mark.parametrize("tokens", ["with a host token", "without one"])
def test_a_project_name_ending_in_a_git_character_is_not_truncated(
    config, monkeypatch, tokens, no_host_tokens
):
    """The literal name, which is the one thing parity above cannot check (T48).

    ``group/subgroup/project`` used to be filed as ``group/subgroup/projec``, so
    every run of such a project — `openpipes/openpipes.net`, `docs/lmer-doc-bot`
    — lived under a directory named after a mangled identity. Both token states,
    because only the token-less SSH spelling was broken and a test that passed on
    both spellings for the wrong reason would prove nothing.
    """
    if tokens == "with a host token":
        monkeypatch.setenv(TOKEN_ENV, HOST_TOKEN)
    request = request_for(repo_url=None, target=GIT_CHAR_TAILED_TARGET)

    _recorded, identity = spawn._repo_urls(request)
    host, project, _slug = spawn.derive_run_identity(request, identity)

    assert (host, project) == ("gitlab.example.com", "group/subgroup/project")


@pytest.mark.parametrize("target", DERIVABLE_TARGETS)
def test_a_target_derived_url_is_never_the_run_s_repo_of_record(config, target):
    """Identity, not evidence — the distinction T33 drew, on a third source.

    Recording it would round-trip (it parses back to the identity it produced), so
    it would satisfy every later check: ``resume`` would stop asking for a repo URL
    for this run forever after, on a URL nobody supplied and whose clone transport
    was decided by whichever token the daemon happened to hold.
    """
    result = spawn.spawn_session(config, request_for(repo_url=None, target=target))

    assert runs.get_tracked(result.host, result.project, result.slug).repo is None


@pytest.mark.parametrize("target", DERIVABLE_TARGETS)
def test_the_entry_of_a_target_identified_session_records_no_repo(
    config, monkeypatch, target
):
    """The index is not the only place a spawn writes a URL it was handed."""
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for(repo_url=None, target=target))
    try:
        assert registry.read_session(result.session_id)["task"]["repo"] is None
    finally:
        os.kill(result.pid, 9)


def test_the_daemon_s_own_repo_url_still_beats_the_target(config, monkeypatch):
    """Evidence before reconstruction: the target is the *last* source, not the first.

    An exported LMER_REPO_URL is the operator naming this daemon's repository, and
    it is recorded; the target is only ever read for an identity. Deriving first
    would file the run under the target's project and record nothing, quietly
    dropping a URL the operator did supply.
    """
    monkeypatch.setenv("LMER_REPO_URL", "https://gitlab.example.com/agents/other.git")
    result = spawn.spawn_session(config, request_for(repo_url=None))

    assert result.project == "agents/other"
    assert runs.get_tracked(result.host, result.project, result.slug).repo == (
        "https://gitlab.example.com/agents/other.git"
    )


def test_a_supplied_identity_url_still_beats_the_target(config):
    """answer.py's reconstruction is a caller stating where the run already belongs.

    It is filed under a run that exists in the index; the target's project is a
    guess about the same session. Deriving over it would hand the answering
    session a second, differently-keyed row.
    """
    result = spawn.spawn_session(config, request_for(
        repo_url=None,
        identity_repo_url="https://gitlab.example.com/agents/elsewhere",
        target=DERIVABLE_TARGETS[0],
    ))

    assert result.project == "agents/elsewhere"


def test_a_no_repo_session_derives_nothing_from_its_target(config, monkeypatch):
    """Spec D17 outranks the target, and the assistant is why.

    It spawns with ``no_repo=True`` and a perfectly derivable target; a session
    that structurally has no checkout must not be filed under the repository its
    target happens to name — that row would claim a run nobody is doing.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(
        config, request_for(repo_url=None, target=DERIVABLE_TARGETS[0], no_repo=True)
    )
    try:
        assert (result.host, result.project) == (None, None)
        assert runs.list_tracked() == []
        assert registry.read_session(result.session_id)["task"]["repo"] is None
    finally:
        os.kill(result.pid, 9)


# --- the credential the derivation hands back --------------------------------
#
# `cli._derive_repo_url_from_task_target` looks a host token up itself and answers
# `https://oauth2:<token>@host/project.git` when it finds one. An orchestrator
# holds tokens for its own forge as a matter of course, so this is the ordinary
# path rather than an edge case — and the value goes on to be logged, parsed and
# (when it does not parse) quoted back to the caller.

def test_the_credential_in_a_derived_url_is_real(host_token):
    """First, that there is something to scrub: the helper's own answer.

    Asserting that spawn.py calls a scrub would only restate spawn.py.
    """
    from lmer_cli.cli import _derive_repo_url_from_task_target

    assert HOST_TOKEN in _derive_repo_url_from_task_target(DERIVABLE_TARGETS[0])


def test_the_identity_url_read_out_of_a_target_carries_no_credential(host_token):
    assert HOST_TOKEN not in spawn._identity_url_from_target(DERIVABLE_TARGETS[0])


def test_scrubbing_the_derived_url_does_not_cost_the_identity(host_token):
    """Which is what makes scrubbing at the source free rather than a trade.

    ``_parse_repo_url`` reads the host out of the netloc after the ``@``, so the
    stripped URL yields the same host and project — and the same run directory.
    """
    from lmer_cli.cli import _derive_repo_url_from_task_target, _parse_repo_url

    target = DERIVABLE_TARGETS[0]
    assert _parse_repo_url(spawn._identity_url_from_target(target)) == _parse_repo_url(
        _derive_repo_url_from_task_target(target)
    )


def test_the_line_that_explains_where_the_identity_came_from_is_safe_to_paste(
    config, caplog, host_token
):
    """A run filed under a project the request never named needs explaining.

    That line is the one thing standing between an operator and a mystery row, so
    it names the URL — which means the URL has to be one a platform log may hold.
    """
    caplog.set_level("INFO")
    spawn.spawn_session(config, request_for(repo_url=None, target=DERIVABLE_TARGETS[0]))

    derived_lines = [
        r for r in caplog.records
        if "platform_spawn_identity_from_target" in r.message
    ]
    assert derived_lines, "nothing said where the identity came from"
    assert all(HOST_TOKEN not in r.getMessage() for r in caplog.records)


def test_a_token_the_derivation_found_reaches_nothing_that_is_written_down(
    config, monkeypatch, host_token
):
    """Every place a spawn leaves a trace, checked for the token in one go.

    argv is in the list because it is world-readable in ``ps`` — the same reason
    the control token travels in the environment — and the run index, the registry
    entry and the event log because those are files people ``cat`` into tickets.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(
        config, request_for(repo_url=None, target=DERIVABLE_TARGETS[0])
    )
    try:
        assert result.project == "agents/global", "the identity was still derived"
        written = "\n".join([
            " ".join(result.command),
            json.dumps(result.to_dict()),
            registry.session_path(result.session_id).read_text(encoding="utf-8"),
            json.dumps([entry.to_dict() for entry in runs.list_tracked()]),
            json.dumps(store.read_events()),
        ])
        assert HOST_TOKEN not in written
        assert "oauth2" not in written
    finally:
        os.kill(result.pid, 9)


def test_no_derived_url_reaches_the_child_s_argv_at_all(config, host_token):
    """Not just the credential: the platform passes the child no repository.

    ``lmer`` derives its own clone URL from the target it is given (which is how
    the two identities agree), so a repo URL in argv would be a second copy of a
    fact the child already has — and, for the tokenised spelling, a credential in
    ``ps``.
    """
    target = DERIVABLE_TARGETS[0]
    result = spawn.spawn_session(config, request_for(repo_url=None, target=target))
    command = " ".join(result.command)

    assert spawn._identity_url_from_target(target) not in command
    assert "--repo" not in command
    # And the child still receives the target the identity came out of, as lmer's
    # own parser reads it.
    namespace, rest = parse_args(list(result.command[1:]))
    assert rest == []
    assert namespace.target == [target]


def test_an_unparseable_repo_url_is_not_quoted_back_with_its_credential(config):
    """The residual message names the URL that failed, so it scrubs it first.

    A credentialed URL with no project (a real typo shape) parses to no identity,
    which is how a token would otherwise reach the warning — and the warning goes
    into the daemon's log *and* the spawn's response body.
    """
    leaky = f"https://oauth2:{HOST_TOKEN}@gitlab.example.com/"
    result = spawn.spawn_session(config, request_for(repo_url=leaky))

    assert result.warning and HOST_TOKEN not in result.warning
    assert "gitlab.example.com" in result.warning, "scrubbed, not swallowed"


# --- the residual case, said out loud ----------------------------------------
#
# A branch name or a sentence names no repository, so nothing can be derived and
# the run still has no identity. That case is not rare and it is not visible: the
# session starts, works and looks healthy, and the only symptom is its row leaving
# the fleet view minutes later. So it is reported where the person who typed the
# target will see it, not only in the daemon's log.

@pytest.mark.parametrize("target", UNDERIVABLE_TARGETS)
def test_a_target_nothing_can_be_derived_from_warns_at_spawn_time(
    config, caplog, monkeypatch, target
):
    # Kept asleep for the registry assertion: a clean exit reaps the entry, which
    # is the very disappearance this test is about.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for(repo_url=None, target=target))
    try:
        assert runs.list_tracked() == []
        assert registry.read_session(result.session_id) is not None, (
            "the session is real and running; it is the *run* that is not recorded"
        )
        assert any(
            "platform_spawn_untracked_run" in r.message for r in caplog.records
        )
    finally:
        os.kill(result.pid, 9)


@pytest.mark.parametrize("target", UNDERIVABLE_TARGETS)
def test_the_warning_names_the_consequence_and_travels_with_the_result(config, target):
    """A log line is what this failure already had, and it is what let it through.

    ``SpawnResult.warning`` is echoed by ``POST /api/sessions``, so the operator who
    left the field blank is told what it cost while the target is still in front of
    them.
    """
    result = spawn.spawn_session(config, request_for(repo_url=None, target=target))

    assert result.warning, "a spawn that lost the run said nothing about it"
    assert "not tracked" in result.warning
    assert "disappears when it exits" in result.warning
    assert target in result.warning, "the operator cannot fix a target it does not name"
    assert result.to_dict()["warning"] == result.warning


def test_an_identified_run_carries_no_warning(config):
    """Otherwise the warning is decoration and gets read as such."""
    result = spawn.spawn_session(config, request_for())

    assert result.warning is None
    assert result.to_dict()["warning"] is None


def test_a_no_repo_session_is_not_warned_about_a_run_it_never_had(config, caplog):
    """Spec D17 is the designed case, not a lost run.

    The assistant spawns one on every daemon start. A warning that fires there too
    is a warning operators learn to skip past — which is how the real one went
    unnoticed in the first place.
    """
    caplog.set_level("INFO")
    result = spawn.spawn_session(config, request_for(repo_url=None, no_repo=True))

    assert result.warning is None
    assert not any(
        "platform_spawn_untracked_run" in r.message for r in caplog.records
    )
    assert any("platform_spawn_no_repo_session" in r.message for r in caplog.records)


# The run dialog's repo-URL prefill, the hint beside that field and the warning the
# dialog repeats used to be pinned here. They are about `GET /api/spawn-options` and
# AddRun.vue, and they landed in this file only because
# tests/test_platform_addrun.py — which owns that route and this form's markup —
# was outside T28's file scope. T48 moved them there: see its "the URL a blank repo
# field falls back to" section and the markup section that follows it. The warning
# itself is still tested here, where it is produced.

# --- exit handling ----------------------------------------------------------
#
# Two situations, two different tools, and picking the wrong one is how three
# tests in this suite came to race the watcher thread:
#
#   "the entry still exists"  -> keep the stub alive (FAKE_LMER_SLEEP), because
#                                there is no event meaning "the exit has NOT
#                                happened yet" and never will be.
#   "the exit was processed"  -> spawn.wait_for_exit_recorded(), which returns
#                                once the watcher has finished recording the
#                                ending — entry removed or kept, transcript
#                                scrubbed. A fact to await, not a poll to win.
#
# Prefer the event over `wait_for(lambda: ...)` where it applies: polling asks
# "has it changed yet?" repeatedly and passes on an idle machine for the wrong
# reason, which is precisely what hid these failures until a gate run.

def test_clean_exit_clears_the_registry_entry(config):
    result = spawn.spawn_session(config, request_for())

    assert spawn.wait_for_exit_recorded(result.session_id), (
        "the watcher never recorded the ending"
    )
    assert registry.read_session(result.session_id) is None


def test_crash_leaves_the_entry_as_the_crash_signal(config, monkeypatch):
    """A non-zero exit must stay visible: that entry is how a crash is detected."""
    monkeypatch.setenv("FAKE_LMER_EXIT", "3")
    result = spawn.spawn_session(config, request_for())

    assert wait_for(
        lambda: any(
            e.get("data", {}).get("session") == result.session_id
            and e["type"] == "session_exited"
            for e in store.read_events()
        )
    )
    entry = registry.read_session(result.session_id)
    assert entry is not None, "a crashed session's entry must survive"
    assert registry.is_live(entry) is False


def test_exit_is_recorded_in_platform_history(config):
    result = spawn.spawn_session(config, request_for())
    assert wait_for(lambda: registry.read_session(result.session_id) is None)

    types = [e["type"] for e in store.read_events()]
    assert "session_spawned" in types
    assert "session_exited" in types


def test_spawn_event_carries_the_run_identity(config):
    result = spawn.spawn_session(config, request_for())
    spawned = [e for e in store.read_events() if e["type"] == "session_spawned"]
    assert spawned[-1]["data"]["run"]["slug"] == result.slug


# --- capacity ---------------------------------------------------------------

def test_concurrency_cap_is_enforced(platform_root, fake_lmer, monkeypatch):
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    # Occupy the single slot with an entry whose PID is this test process.
    registry.register("s-occupant", pid=os.getpid())

    with pytest.raises(spawn.CapacityError, match="1/1"):
        spawn.spawn_session(config, request_for())


def test_dead_sessions_do_not_count_against_the_cap(platform_root, fake_lmer):
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    registry.register("s-dead", pid=2**22)

    result = spawn.spawn_session(config, request_for())
    assert result.session_id


def test_capacity_error_is_a_spawn_error(config):
    assert issubclass(spawn.CapacityError, spawn.SpawnError)


def test_the_assistant_does_not_occupy_a_worker_slot(platform_root, fake_lmer):
    """``max_concurrent_sessions`` counts workers (T75).

    The platform starts one assistant for itself at boot, so counting it would
    silently make a host configured for one session a host that runs no work at
    all. The reservation lives here rather than in
    :mod:`lmer_platform.assistant` because worker spawns come through this
    function too — anywhere else, the slot would not be reserved *from* them.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    registry.register(
        "s-assistant", kind=registry.ASSISTANT_KIND, pid=os.getpid()
    )

    assert spawn._live_worker_count() == 0
    result = spawn.spawn_session(config, request_for())
    assert result.session_id, "the assistant was holding the only worker slot"


def test_the_assistants_own_spawn_is_not_refused_by_its_predecessors_entry(
    platform_root, fake_lmer
):
    """The exclusion is by kind, so it runs in both directions — one rule.

    A live assistant entry (a previous incarnation, or a start racing another)
    must not be what refuses an assistant spawn either. What refuses a *second*
    assistant is ``assistant.start``'s registry check (D11), which is a different
    file and a different question from how many workers this host may run.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    registry.register(
        "s-assistant", kind=registry.ASSISTANT_KIND, pid=os.getpid()
    )

    result = spawn.spawn_session(
        config, request_for(), kind=registry.ASSISTANT_KIND
    )
    assert result.session_id


def test_workers_are_still_counted_one_by_one(platform_root, fake_lmer):
    """The paired half of the exemption: the cap must still be a cap.

    Without this, "the assistant does not count" could pass on a count that had
    quietly stopped counting anything.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 2})
    registry.register("s-assistant", kind=registry.ASSISTANT_KIND, pid=os.getpid())
    registry.register("s-worker-1", kind=registry.WORKER_KIND, pid=os.getpid())
    registry.register("s-worker-2", kind=registry.WORKER_KIND, pid=os.getpid())

    assert spawn._live_worker_count() == 2
    with pytest.raises(spawn.CapacityError, match="2/2"):
        spawn.spawn_session(config, request_for())


# --- one run, one session ----------------------------------------------------
#
# From the field: an operator answered a run's pending question after that
# session had ended, and half a minute later a second session for the same run
# appeared with nobody asking for it. Answering spawns by design, so that half was
# working — what was missing was a floor under it. Two callers each carried their
# own live-run check and ``POST /api/sessions``, which derives the same identity
# from the same taskdef and target, carried none at all, so a duplicate session for
# one run was a plain API call away.
#
# The invariant is therefore tested *here*, on the function every fleet-visible
# session comes through, rather than only through the verbs that respawn a run.

def identity_of(request, repo_url="https://gitlab.example.com/agents/global.git"):
    """The run a spawn of *request* would file itself under.

    Read out of the platform's own prediction rather than spelled out in the test:
    a hand-written slug would let the invariant pass on an identity no spawn
    actually derives.
    """
    return spawn.derive_run_identity(request, repo_url)


def test_a_run_with_a_live_session_is_not_spawned_a_second_time(config):
    host, project, slug = identity_of(request_for())
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": host, "project": project, "slug": slug},
    )

    with pytest.raises(spawn.RunAlreadyLive):
        spawn.spawn_session(config, request_for())
    assert len(registry.list_sessions(live_only=False)) == 1, (
        "the refused spawn registered a session anyway"
    )


def test_the_refusal_names_the_session_that_holds_the_run(config):
    """An operator or assistant reading the 409 has to know where the run went.

    Three things, and the identification is asserted where it is *made* rather than
    anywhere in the sentence: the run key says which run is held, ``(<id>, pid <n>)``
    says which session holds it, and the route says what to do instead. Matching a
    bare id would pass on a message that only mentioned it in the remedy — a
    refusal that names a session to type into without saying it is the holder.
    """
    host, project, slug = identity_of(request_for())
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": host, "project": project, "slug": slug},
    )

    with pytest.raises(spawn.RunAlreadyLive) as caught:
        spawn.spawn_session(config, request_for())

    message = str(caught.value)
    assert message.startswith(f"{runs.run_key(host, project, slug)} already has a live")
    assert f"(s-live, pid {os.getpid()})" in message
    assert "POST /api/sessions/s-live/input" in message


def test_run_already_live_is_a_spawn_error(config):
    """So a caller that only knows ``SpawnError`` still refuses rather than 500s.

    The API maps it ahead of that base class, which is the only way it reaches the
    operator as a 409 — see tests/test_platform_api.py.
    """
    assert issubclass(spawn.RunAlreadyLive, spawn.SpawnError)


def test_a_dead_entry_never_blocks_a_respawn(config):
    """The crash shape, and it must not wedge the run out of ever running again.

    A crashed session's entry is kept deliberately as the crash signal, so "an
    entry names this run" cannot be the test — liveness is, and the registry reads
    it from the pid on every read.
    """
    host, project, slug = identity_of(request_for())
    registry.register(
        "s-dead", pid=2**22,
        run={"host": host, "project": project, "slug": slug},
    )

    result = spawn.spawn_session(config, request_for())
    assert result.session_id, "a dead entry refused a respawn of its run"


def test_another_run_on_the_same_host_is_not_refused(config, monkeypatch):
    """The paired half: the invariant must be about one run, not about the host.

    Without this, "one run, one session" could pass as "one session".
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    first = spawn.spawn_session(config, request_for())
    try:
        second = spawn.spawn_session(config, request_for(
            target="https://gitlab.example.com/agents/global/-/work_items/142"
        ))
        assert second.slug != first.slug
        os.kill(second.pid, 9)
    finally:
        os.kill(first.pid, 9)


@pytest.mark.parametrize("differs", ["host", "project"])
def test_the_same_slug_under_another_project_is_a_different_run(config, differs):
    """A run is the whole triple, so a slug collision across projects is not one.

    Slugs are derived from a taskdef and a target and two forges can hand out the
    same one; refusing on the slug alone would make one project's live session
    block another project's run.
    """
    host, project, slug = identity_of(request_for())
    other = {"host": host, "project": project, "slug": slug}
    other[differs] = "elsewhere/entirely"
    registry.register("s-live", pid=os.getpid(), run=other)

    result = spawn.spawn_session(config, request_for())
    assert result.session_id, "another project's session refused this run"


def test_a_session_with_no_run_identity_does_not_hold_a_run(config, monkeypatch):
    """Two no-repo sessions (spec D17) are not two sessions for one run.

    They have no run at all — nothing is tracked for them and nothing keys on them
    — so an identity of ``(None, None, slug)`` must match nothing. This is also
    what leaves the assistant unaffected: it spawns ``no_repo``, so its host and
    project are unset and its own supervisor's respawn cannot be refused here. What
    refuses a *second* assistant is ``assistant.start``'s registry check (D11),
    which is a different file and a different question.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    first = spawn.spawn_session(config, request_for(repo_url=None, no_repo=True))
    try:
        entry = registry.read_session(first.session_id)
        assert entry["run"]["host"] is None, "this test's premise is gone"

        second = spawn.spawn_session(
            config, request_for(repo_url=None, no_repo=True)
        )
        assert second.slug == first.slug, "the two must derive the same slug"
        os.kill(second.pid, 9)
    finally:
        os.kill(first.pid, 9)


def platform_paths():
    """Every path under the platform state dir — the litter check's instrument."""
    root = Path(store.PLATFORM_DIR)
    return sorted(str(path) for path in root.rglob("*")) if root.is_dir() else []


def test_the_refused_spawn_leaves_no_session_state_behind(config):
    """Refused before the command is built, so there is nothing to clean up.

    Asserted on the state directory rather than in a comment: a token minted or a
    log directory made for a session that was never started is litter, and one of
    those two is a credential.
    """
    host, project, slug = identity_of(request_for())
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": host, "project": project, "slug": slug},
    )
    before = platform_paths()

    with pytest.raises(spawn.RunAlreadyLive):
        spawn.spawn_session(config, request_for())

    assert platform_paths() == before


# --- port reporting ---------------------------------------------------------

def test_child_is_told_where_to_report_ports(config, monkeypatch):
    monkeypatch.setenv(
        "FAKE_LMER_PORTS_JSON",
        json.dumps({"bind": "127.0.0.1", "ports": [{"host": 30021, "container": 30021}]}),
    )
    # Keep the session alive, same as the absorb test below: a clean exit unlinks
    # the ports file, and a stub that writes it and exits immediately loses that
    # race often enough to matter (~15% of runs — the file was already gone by the
    # time spawn_session returned).
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for(ports=1))
    try:
        ports_file = spawn.ports_file_for(result.session_id)
        assert wait_for(lambda: ports_file.is_file())
        assert json.loads(
            ports_file.read_text(encoding="utf-8")
        )["ports"][0]["host"] == 30021
    finally:
        os.kill(result.pid, 9)


def test_absorb_ports_folds_the_mapping_into_the_registry(config, monkeypatch):
    monkeypatch.setenv(
        "FAKE_LMER_PORTS_JSON",
        json.dumps({"ports": [{"host": 30022, "container": 3000}]}),
    )
    # Keep the session alive: a clean exit reaps its registry entry, which would
    # race the assertion below (this test failed only under full-suite load).
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for(ports=1))
    try:
        assert wait_for(lambda: spawn.ports_file_for(result.session_id).is_file())

        absorbed = spawn.absorb_ports(registry.list_sessions(live_only=False))
        mine = [e for e in absorbed if e.get("id") == result.session_id]
        assert mine and mine[0]["ports"] == [{"host": 30022, "container": 3000}]
    finally:
        os.kill(result.pid, 9)


def test_absorb_ports_leaves_existing_mappings_alone(platform_root):
    registry.register("s-1", pid=os.getpid(), ports=[{"host": 1, "container": 1}])
    spawn.ports_file_for("s-1").parent.mkdir(parents=True, exist_ok=True)
    spawn.ports_file_for("s-1").write_text(
        json.dumps({"ports": [{"host": 9, "container": 9}]}), encoding="utf-8"
    )

    absorbed = spawn.absorb_ports(registry.list_sessions())
    assert absorbed[0]["ports"] == [{"host": 1, "container": 1}]


def test_absorb_ports_tolerates_missing_and_corrupt_files(platform_root):
    registry.register("s-none", pid=os.getpid())
    registry.register("s-bad", pid=os.getpid())
    spawn.ports_file_for("s-bad").parent.mkdir(parents=True, exist_ok=True)
    spawn.ports_file_for("s-bad").write_text("{not json", encoding="utf-8")

    absorbed = spawn.absorb_ports(registry.list_sessions())
    assert all(not e.get("ports") for e in absorbed)


def test_absorb_ports_ignores_non_dict_entries(platform_root):
    assert spawn.absorb_ports(["nonsense", None]) == ["nonsense", None]


def test_absorb_ports_ignores_an_empty_port_list(platform_root):
    registry.register("s-1", pid=os.getpid())
    spawn.ports_file_for("s-1").parent.mkdir(parents=True, exist_ok=True)
    spawn.ports_file_for("s-1").write_text(json.dumps({"ports": []}), encoding="utf-8")
    assert not spawn.absorb_ports(registry.list_sessions())[0].get("ports")


# --- the control plane (spec D8) ---------------------------------------------
#
# Every spawned session must be reachable *and* writable, which means the port and
# the token have to be known to the platform from the first moment the entry
# exists. These tests hold the two halves of that apart: the values really reach
# the child's environment, and the credential really does not reach the entry.
#
# A clean-exiting stub tears its own token file down, so a test that asserts on
# live control state keeps the session asleep and kills it in a `finally`.

@pytest.fixture
def env_dumping_lmer(tmp_path):
    """A stub that records the environment it was actually launched with.

    The control facts are handed down through the child's environment, so the
    assertion belongs on what the process received — not on what the spawn code
    intended to pass.
    """
    dump = tmp_path / "child-env.txt"
    script = tmp_path / "env-lmer"
    script.write_text(
        "#!/bin/sh\n"
        f'env > "{dump}"\n'
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, dump


def env_from_dump(dump):
    """Parse `env` output; continuation lines of multi-line values are ignored."""
    values = {}
    for line in dump.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name:
            values[name] = value
    return values


def test_child_env_carries_the_port_and_token_the_platform_chose(
    platform_root, env_dumping_lmer, monkeypatch
):
    script, dump = env_dumping_lmer
    config = cfg.load({"lmer_bin": str(script)})
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        assert wait_for(lambda: dump.is_file() and dump.stat().st_size)
        child = env_from_dump(dump)
        entry = registry.read_session(result.session_id)

        assert child["LMER_FASTAPI_PORT"] == str(entry["control"]["port"]), (
            "the entry's port must be the one the session was told to bind"
        )
        assert child["LMER_FASTAPI_TOKEN"] == spawn.read_control_token(
            result.session_id
        ), "the token on disk must be the one the session accepts"
        assert child["LMER_PLATFORM_PORTS_FILE"] == str(
            spawn.ports_file_for(result.session_id)
        ), "the control vars must not have displaced the ports file"
    finally:
        os.kill(result.pid, 9)


def test_a_no_repo_session_is_told_so_in_its_environment(
    platform_root, env_dumping_lmer, monkeypatch
):
    """D17's mechanism, on the environment the child actually received.

    ``lmer`` reads LMER_NO_REPO from its own environment: it skips repo
    resolution on it (which is what lets a target that is not a repository be
    given at all) and passes it into the container, where ``clone_and_exec``
    skips the workspace clone. A flag would not do — there is none — and a
    session told nothing is a session with a checkout.
    """
    script, dump = env_dumping_lmer
    config = cfg.load({"lmer_bin": str(script)})
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for(repo_url=None, no_repo=True))
    try:
        assert wait_for(lambda: dump.is_file() and dump.stat().st_size)
        assert env_from_dump(dump).get(spawn.NO_REPO_ENV) == "1"
    finally:
        os.kill(result.pid, 9)


def test_an_ordinary_session_never_inherits_the_daemons_no_repo(
    platform_root, env_dumping_lmer, monkeypatch
):
    """Inheriting it would hand every worker an empty /workspace, silently.

    The request still names a repository, so nothing downstream would look wrong
    — the session would simply have no code to work on. Same reasoning as the ask
    channel's variable, one step sharper.
    """
    script, dump = env_dumping_lmer
    config = cfg.load({"lmer_bin": str(script)})
    monkeypatch.setenv(spawn.NO_REPO_ENV, "1")
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        assert wait_for(lambda: dump.is_file() and dump.stat().st_size)
        assert spawn.NO_REPO_ENV not in env_from_dump(dump)
    finally:
        os.kill(result.pid, 9)


@pytest.fixture
def resolving_lmer(tmp_path):
    """A stub that resolves its control plane the way the real host CLI does.

    The contract this slice rests on spans two packages: the platform hands the
    port and token down through the environment, and ``lmer`` has to *use* them
    rather than choosing its own. Asserting on what spawn.py passed would only
    restate spawn.py. So this stub calls ``cli._resolve_fastapi_host_port`` and
    the same ``--fastapi-token``-or-env fallback the CLI's container env dict
    uses, then reports what it got.

    Runs under ``sys.executable`` so it lands in the interpreter the test suite
    is using, whatever venv that is.
    """
    dump = tmp_path / "resolved.json"
    script = tmp_path / "resolving-lmer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "from lmer_cli.cli import _resolve_fastapi_host_port\n"
        "resolved = {\n"
        "    'port': _resolve_fastapi_host_port(None, os.environ),\n"
        # `None` stands in for an absent --fastapi-token, mirroring cli.py's
        # "LMER_FASTAPI_TOKEN": ns.fastapi_token or os.environ.get(...)
        "    'token': None or os.environ.get('LMER_FASTAPI_TOKEN'),\n"
        "}\n"
        f"open({str(dump)!r}, 'w').write(json.dumps(resolved))\n"
        "import time; time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, dump


def test_lmer_resolves_the_very_port_and_token_the_entry_advertises(
    platform_root, resolving_lmer
):
    """The whole point: no daylight between the entry and the running session.

    Before the CLI honored these vars it picked its own port and let the
    supervisor mint its own token, so the entry described a control plane that
    did not exist — reachable-looking, and wrong only at the moment someone tried
    to use it.
    """
    script, dump = resolving_lmer
    config = cfg.load({"lmer_bin": str(script)})
    result = spawn.spawn_session(config, request_for())
    try:
        assert wait_for(lambda: dump.is_file() and dump.stat().st_size)
        resolved = json.loads(dump.read_text(encoding="utf-8"))
        entry = registry.read_session(result.session_id)

        assert resolved["port"] == entry["control"]["port"] == result.control_port
        assert resolved["token"] == spawn.read_control_token(result.session_id)
    finally:
        os.kill(result.pid, 9)


def test_token_file_is_created_unreadable_to_other_users(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        path = spawn.token_file_for(result.session_id)
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, (
            "0600 at creation — a token that is briefly world-readable has leaked"
        )
        assert path.read_text(encoding="utf-8").strip()
    finally:
        os.kill(result.pid, 9)


def test_the_entry_points_at_the_token_and_never_holds_it(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        control = registry.read_session(result.session_id)["control"]
        assert control["host"] == "127.0.0.1"
        assert isinstance(control["port"], int)
        assert control["token_ref"] == str(spawn.token_file_for(result.session_id))

        token = spawn.read_control_token(result.session_id)
        raw = registry.session_path(result.session_id).read_text(encoding="utf-8")
        assert token and token not in raw, (
            "registry files get pasted into tickets — only the token_ref may be in one"
        )
        assert token not in " ".join(result.command), "argv is visible in `ps`"
    finally:
        os.kill(result.pid, 9)


def test_read_control_token_round_trips(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        token = spawn.read_control_token(result.session_id)
        assert token
        assert spawn.token_file_for(result.session_id).read_text(
            encoding="utf-8"
        ) == token
    finally:
        os.kill(result.pid, 9)


def test_read_control_token_is_none_when_there_is_no_token(platform_root):
    """Absent is a normal answer: the caller cannot drive the session either way."""
    assert spawn.read_control_token("s-never-existed") is None


def test_concurrent_sessions_get_distinct_control_ports(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    first = spawn.spawn_session(config, request_for())
    second = spawn.spawn_session(
        config, request_for(target="https://gitlab.example.com/agents/global/-/work_items/142")
    )
    try:
        ports = {
            registry.read_session(r.session_id)["control"]["port"]
            for r in (first, second)
        }
        assert len(ports) == 2, (
            "two live sessions on one port means the second cannot publish it"
        )
    finally:
        for result in (first, second):
            os.kill(result.pid, 9)


def test_a_port_a_live_session_claimed_is_not_handed_out_again(
    platform_root, monkeypatch
):
    """``_pick_port`` releases its test bind, so the registry is the only claim.

    Drives the picker directly because the collision it guards against is a
    coincidence of two random draws — not something a spawn can be asked for.
    """
    registry.register(
        "s-holder",
        pid=os.getpid(),
        control={"host": "127.0.0.1", "port": 8777, "token_ref": "/dev/null"},
    )
    draws = iter([8777, 8777, 8781])
    monkeypatch.setattr(spawn, "_pick_port", lambda *_a, **_kw: next(draws))

    assert spawn._pick_control_port() == 8781


@pytest.mark.parametrize("bad_arg", [
    "--fastapi-token", "--fastapi-token=abc", "--fastapi-host",
    "--fastapi-port-range", "--no-supervisor",
    # argparse accepts unambiguous abbreviations, so the short spellings are
    # every bit as effective as the full ones.
    "--no-super", "--fastapi-tok",
])
def test_reserved_control_plane_args_are_refused(config, bad_arg):
    """extra_args comes from the POST body, so this is reachable input."""
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(config, request_for(extra_args=(bad_arg, "x")))

    assert registry.list_sessions(live_only=False) == [], (
        "a request that would desync the control plane must not start a session"
    )


@pytest.mark.parametrize("bad_arg", [
    "--answer", "--answer=yes", "--answ=yes", "--ans", "--a=yes",
])
def test_an_answer_in_extra_args_is_refused(config, bad_arg):
    """A caller with the shared secret must not be able to answer a run this way.

    lmer_platform.answer checks that a question is actually open, that the respawn
    derives the same run, and that no live session is already working it. A raw
    spawn checks none of that — so the flag is reserved here, abbreviations
    included, and the answer path uses the typed field instead.
    """
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(config, request_for(extra_args=(bad_arg, "x")))
    assert registry.list_sessions(live_only=False) == []


def test_the_answer_refusal_names_the_way_through(config):
    """Every refusal here says what to do instead; this one has a real answer."""
    with pytest.raises(spawn.SpawnError, match="POST /api/runs/answer"):
        spawn.spawn_session(config, request_for(extra_args=("--answer=yes",)))


# The prefix-guard's "merely starts like a reserved flag" case used to be pinned
# here with `--agents` against `--answer`. T37 made `--agents` a typed field the
# platform emits and records, so it joined _RESERVED_ARGS — and since `--answer`
# and `--agents` are now lmer's only two `--a` flags, the case can no longer be
# built from that pair at all. It is not lost: it moved to
# tests/test_platform_addrun.py::test_the_direction_flag_still_rides_through_beside_the_reserved_preset,
# where `--prompt` plays it against `--preset` and the assertion runs through
# lmer's own parser rather than over the built list. That pairing is also
# load-bearing — lmer_platform.resume depends on `--prompt` reaching the child.


def test_an_answer_after_a_double_dash_is_the_container_s_business(config):
    """The scan stops at `--`, and the addition to the list inherits that."""
    result = spawn.spawn_session(
        config, request_for(extra_args=("--", "echo", "--answer=yes"))
    )
    assert result.command[-3:] == ["--", "echo", "--answer=yes"]


def test_the_answer_field_becomes_one_flag_token_before_extra_args(config):
    """The typed field is the only way in, and where it lands matters twice.

    One token because `--answer -yes` makes argparse exit 2 — an answer starting
    with a dash reads as an option. Before extra_args because those may end in a
    bare `--`, and everything after that belongs to the container's command line.
    """
    result = spawn.spawn_session(
        config,
        request_for(answer="-yes, do it", extra_args=("--", "echo", "hi")),
    )
    assert "--answer=-yes, do it" in result.command
    assert result.command.index("--answer=-yes, do it") < result.command.index("--")
    assert "--answer" not in result.command, (
        "a bare --answer token means the text was passed as a separate argument"
    )


@pytest.mark.parametrize("bad", ["", "   ", 5, ["yes"]])
def test_an_unusable_answer_field_is_refused(config, bad):
    """`--answer=` with nothing behind it, or a repr, would reach lmer as text."""
    with pytest.raises(spawn.SpawnError, match="answer must be non-empty text"):
        spawn.spawn_session(config, request_for(answer=bad))


def test_no_answer_flag_without_an_answer(config):
    assert not any(
        arg.startswith("--answer") for arg in spawn.spawn_session(config, request_for()).command
    )


def test_a_reserved_arg_after_a_double_dash_is_the_container_s_business(config):
    """Everything after `--` is the container command, not lmer's own parser."""
    result = spawn.spawn_session(
        config, request_for(extra_args=("--", "echo", "--no-supervisor"))
    )
    assert result.command[-3:] == ["--", "echo", "--no-supervisor"]


def test_restating_fastapi_is_allowed(config):
    """It is a flag in its own right, and it is what this module passes anyway."""
    result = spawn.spawn_session(config, request_for(extra_args=("--fastapi",)))
    assert result.command.count("--fastapi") == 2


# --- the same guard, one argv position earlier (T32) -------------------------
#
# `_reject_reserved_args` scans extra_args only, but POST /api/sessions takes
# `taskdef` and `target` verbatim from the body too — and `_build_command` puts
# both *ahead* of --fastapi. Spelling a reserved flag there walks straight around
# the guard. It needs the shared secret, so this is not an unauthenticated hole;
# the guard exists to keep the registry honest about the control plane it
# advertises, and that argument does not care how the caller got in.

#: Reserved flags in the spellings a caller would reach for, including the
#: abbreviations argparse accepts (as effective as the full ones) and a bare
#: `--`, which would make everything after it the container's command line.
SMUGGLED_FLAGS = [
    "--fastapi-token=known", "--fastapi-token", "--fastapi-host=0.0.0.0",
    "--fastapi-port-range=9000-9001", "--no-supervisor", "--no-super",
    "--answer=smuggled", "--ans=smuggled", "--", "-x",
]


def test_the_bypass_the_positional_check_closes_is_real(config):
    """``lmer``'s own parser, on the argv ``_build_command`` emits.

    Asserting that spawn.py refuses a dash would only restate spawn.py; this is
    why the refusal is there. The taskdef is consumed as the flag and the target
    slides into the `task` positional, so the session comes up with a control
    plane whose token the caller chose — while the registry entry advertises the
    one the platform minted.
    """
    ns, rest = parse_args(["--fastapi-token=known", request_for().target, "--fastapi"])

    assert ns.fastapi_token == "known", "argparse read the taskdef as a flag"
    assert ns.task == request_for().target, "and the target slid into its place"
    assert rest == [], "nothing was left over to look suspicious"


@pytest.mark.parametrize("smuggled", SMUGGLED_FLAGS)
def test_a_reserved_flag_smuggled_through_the_taskdef_is_refused(config, smuggled):
    with pytest.raises(spawn.SpawnError, match="taskdef may not begin with a dash"):
        spawn.spawn_session(config, request_for(taskdef=smuggled))
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("smuggled", SMUGGLED_FLAGS)
def test_a_reserved_flag_smuggled_through_the_target_is_refused(config, smuggled):
    """One position further along, where it displaces nothing and still parses."""
    with pytest.raises(spawn.SpawnError, match="target may not begin with a dash"):
        spawn.spawn_session(config, request_for(target=smuggled))
    assert registry.list_sessions(live_only=False) == []


def test_the_taskdef_reaches_the_run_dir_name_unsanitized(config):
    """Why the taskdef is checked as a path and not only as an argv positional.

    ``derive_slug`` interpolates it into the run's directory name as given; only
    the *target* passes through ``sanitize_task_target``.
    """
    assert run_state.derive_slug("..", None) == ".."
    assert run_state.derive_slug("../..", "branch-x") == "../..-branch-x"


@pytest.mark.parametrize("taskdef", [
    "..", "../evil", "../../etc/passwd", "develop/../..", "runs/develop",
    "..\\windows", "a\\b", "develop/..",
])
def test_a_taskdef_that_would_escape_the_run_tree_is_refused(config, taskdef):
    with pytest.raises(spawn.SpawnError, match="not a usable path"):
        spawn.spawn_session(config, request_for(taskdef=taskdef))
    assert registry.list_sessions(live_only=False) == []


def test_an_ordinary_taskdef_and_target_are_untouched_by_either_check(config):
    """The guards must not cost the dots and dashes real values are full of."""
    result = spawn.spawn_session(
        config, request_for(taskdef="review-mr", target="v1.2.3-rc.1")
    )
    assert result.command[1:3] == ["review-mr", "v1.2.3-rc.1"]


def test_a_smuggled_flag_is_refused_before_anything_is_started(config, monkeypatch):
    """The check is in ``validate()``, which runs first — nothing is drawn, forked
    or written for a request that is refused."""
    def unreachable(*_args, **_kwargs):
        raise AssertionError("a refused request must not get this far")

    monkeypatch.setattr(spawn, "_pick_port", unreachable)
    monkeypatch.setattr(spawn.subprocess, "Popen", unreachable)

    with pytest.raises(spawn.SpawnError, match="begin with a dash"):
        spawn.spawn_session(config, request_for(taskdef="--fastapi-token=known"))

    assert registry.list_sessions(live_only=False) == []
    assert not list(store.sessions_dir().glob("*.token"))
    assert runs.list_tracked() == []


def test_the_result_says_where_the_session_answers(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        payload = result.to_dict()
        entry = registry.read_session(result.session_id)
        assert payload["control"] == {
            "host": entry["control"]["host"], "port": entry["control"]["port"],
        }
        assert "token" not in payload["control"], (
            "to_dict is the spawn API's response body — the token stays a file"
        )
        assert spawn.read_control_token(result.session_id) not in json.dumps(payload)
    finally:
        os.kill(result.pid, 9)


def test_clean_exit_takes_the_token_with_it(config):
    result = spawn.spawn_session(config, request_for())
    assert wait_for(lambda: not spawn.token_file_for(result.session_id).exists())
    assert spawn.read_control_token(result.session_id) is None


def test_a_crashed_session_keeps_its_token(config, monkeypatch):
    """Its entry survives as the crash signal, so the control info must too.

    Removing the token while leaving the entry would advertise a session as
    controllable and then refuse to control it.
    """
    monkeypatch.setenv("FAKE_LMER_EXIT", "3")
    result = spawn.spawn_session(config, request_for())

    assert wait_for(
        lambda: any(
            e.get("data", {}).get("session") == result.session_id
            and e["type"] == "session_exited"
            for e in store.read_events()
        )
    )
    assert registry.read_session(result.session_id) is not None
    assert spawn.read_control_token(result.session_id), (
        "a retained entry's token_ref must still resolve"
    )


def test_an_unreachable_session_is_not_spawned_at_all(config, monkeypatch):
    """A port-less session would look healthy in the fleet view and answer nothing."""
    def no_free_port(*_args, **_kwargs):
        raise RuntimeError("no free port in range 8700-8799 on 127.0.0.1")

    monkeypatch.setattr(spawn, "_pick_port", no_free_port)

    with pytest.raises(spawn.SpawnError, match="control-plane port"):
        spawn.spawn_session(config, request_for())

    assert registry.list_sessions(live_only=False) == []
    assert not list(store.sessions_dir().glob("*.token"))


def test_a_session_that_cannot_start_leaves_no_token_behind(platform_root, tmp_path):
    config = cfg.load({"lmer_bin": str(tmp_path / "does-not-exist")})
    with pytest.raises(spawn.SpawnError, match="cannot start"):
        spawn.spawn_session(config, request_for())
    assert not list(store.sessions_dir().glob("*.token"))


# --- the harness transcript (T22) --------------------------------------------
#
# `lmer` runs its container with --rm and nothing bind-mounts ~/.claude, so the
# JSONL the chat view reads used to die with the container: T18's view was built
# against a source that had no data. A spawn now mounts a per-session host
# directory in as the harness's projects dir — and because that file outlives the
# session, it gets scrubbed and locked down when the session ends.
#
# The container cannot be launched here (no podman-in-podman), so the stub writes
# the transcript into the host directory named by the --mount-dir argument it
# received. That is what makes these tests about the real argument rather than
# about a string spawn.py intended to build.

#: What a session leaves behind: a credential the agent put in a command line,
#: and prose that has to survive the scrub.
TRANSCRIPT_SECRET = "s3cr3t-value-here"
SESSION_TRANSCRIPT = "\n".join([
    json.dumps({
        "type": "user",
        "timestamp": "2026-07-26T09:00:00.000Z",
        "message": {"role": "user", "content": "start\n/start"},
    }),
    json.dumps({
        "type": "assistant",
        "timestamp": "2026-07-26T09:00:09.000Z",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "cloning the repo now"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {
                "command": f"lmer chat . --fastapi-token {TRANSCRIPT_SECRET}",
            }},
        ]},
    }),
]) + "\n"


def transcript_file(session_id):
    """Where the stub's transcript lands, one level down like the harness's."""
    return spawn.transcript_dir_for(session_id) / "-workspace" / "session.jsonl"


#: Every container destination a spawn mounts a platform-owned directory at. Held
#: as a set rather than a count so a test that adds a mount has to say which one,
#: and so "the mounts do not collide" keeps meaning all of them.
PLATFORM_MOUNT_DESTINATIONS = {
    transcripts.CONTAINER_TRANSCRIPT_DIR,
    ask.CONTAINER_ASK_DIR,
    supervisor.CONTAINER_SESSION_LOG_DIR,
}


def mount_specs_in(command):
    """Every ``--mount-dir`` value in *command*, in order.

    A spawn passes one per platform-owned directory (transcript, ask channel,
    the session's own log), so a test that wants a particular mount selects it by
    destination rather than by position — which is also what stops a reordering
    from silently passing.
    """
    return [
        command[index + 1]
        for index, arg in enumerate(command)
        if arg == "--mount-dir" and index + 1 < len(command)
    ]


def mount_spec_for(command, container_dir):
    """The single ``--mount-dir`` value aimed at *container_dir*."""
    matching = [
        spec for spec in mount_specs_in(command)
        if spec.split(":")[1:2] == [container_dir]
    ]
    assert len(matching) == 1, f"expected one mount at {container_dir}, got {matching}"
    return matching[0]


def test_the_transcript_directory_exists_before_the_session_starts(config):
    """The container mounts it; a directory created afterwards is too late."""
    result = spawn.spawn_session(config, request_for())
    directory = spawn.transcript_dir_for(result.session_id)
    assert directory.is_dir()
    assert directory.parent == store.logs_dir(), "beside the PTY log, by design"


def test_the_transcript_directory_is_private_to_this_user(config):
    """It holds everything the session said, and it is rw-mounted into a container."""
    result = spawn.spawn_session(config, request_for())
    mode = stat.S_IMODE(spawn.transcript_dir_for(result.session_id).stat().st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_the_mount_argument_satisfies_lmer_s_own_validator(config):
    """The two halves of this feature live in different packages.

    Asserting on the string spawn.py built would only restate spawn.py, so the
    argument is handed to the CLI's own parser — which is what will actually read
    it, and which refuses a host path that is not an existing directory.
    """
    result = spawn.spawn_session(config, request_for())
    spec = mount_spec_for(result.command, transcripts.CONTAINER_TRANSCRIPT_DIR)
    specs = parse_dir_mount_specs([spec], "")
    assert len(specs) == 1
    assert specs[0].host == spawn.transcript_dir_for(result.session_id)
    assert specs[0].container == transcripts.CONTAINER_TRANSCRIPT_DIR
    assert specs[0].mode == "rw", "the harness writes into it for the whole session"


def test_the_mount_argument_precedes_a_container_command(config):
    """Everything after `--` is the container's command line, not lmer's argv.

    Appending the flag would put it there, where it is an argument to `echo` and
    the mount silently does not happen.
    """
    result = spawn.spawn_session(
        config, request_for(extra_args=("--", "echo", "hi"))
    )
    assert result.command.index("--mount-dir") < result.command.index("--")


def test_the_mount_argument_survives_a_restated_fastapi_flag(config):
    """The insertion point is the platform's --fastapi, not a caller's.

    One insertion for all of the platform's mounts, so a caller restating the
    flag can neither move them nor split them apart.
    """
    result = spawn.spawn_session(config, request_for(extra_args=("--fastapi",)))
    specs = mount_specs_in(result.command)
    assert len(specs) == len(PLATFORM_MOUNT_DESTINATIONS), specs
    assert result.command.index("--mount-dir") < len(result.command) - 1
    assert mount_spec_for(result.command, transcripts.CONTAINER_TRANSCRIPT_DIR)


def test_the_transcript_a_session_wrote_outlives_it_and_is_scrubbed(config, monkeypatch):
    """The two halves of the point: the file is still there, and it is masked."""
    monkeypatch.setenv("FAKE_LMER_TRANSCRIPT", SESSION_TRANSCRIPT)
    result = spawn.spawn_session(config, request_for())
    target = transcript_file(result.session_id)

    assert wait_for(lambda: target.is_file()), "the stub wrote where the mount pointed"
    assert wait_for(
        lambda: TRANSCRIPT_SECRET not in target.read_text(encoding="utf-8")
    ), "the credential must be gone once the session has exited"

    text = target.read_text(encoding="utf-8")
    assert "cloning the repo now" in text, "scrubbed, not emptied"
    for line in text.splitlines():
        assert isinstance(json.loads(line), dict), "still valid JSONL"


def test_the_scrubbed_transcript_is_left_owner_only(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_TRANSCRIPT", SESSION_TRANSCRIPT)
    result = spawn.spawn_session(config, request_for())
    target = transcript_file(result.session_id)
    assert wait_for(
        lambda: target.is_file()
        and TRANSCRIPT_SECRET not in target.read_text(encoding="utf-8")
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_scrub_leaves_no_temp_file_behind(config, monkeypatch):
    """A leftover temp would be an unscrubbed copy of what was just scrubbed."""
    monkeypatch.setenv("FAKE_LMER_TRANSCRIPT", SESSION_TRANSCRIPT)
    result = spawn.spawn_session(config, request_for())
    target = transcript_file(result.session_id)
    assert wait_for(
        lambda: target.is_file()
        and TRANSCRIPT_SECRET not in target.read_text(encoding="utf-8")
    )
    leftovers = list(spawn.transcript_dir_for(result.session_id).rglob(".*.tmp"))
    assert leftovers == []


def test_the_chat_view_can_read_what_the_session_left(config, monkeypatch):
    """What T22 is for: the view T18 built finally has a source with data in it."""
    monkeypatch.setenv("FAKE_LMER_TRANSCRIPT", SESSION_TRANSCRIPT)
    result = spawn.spawn_session(config, request_for())
    assert wait_for(lambda: transcript_file(result.session_id).is_file())
    assert wait_for(lambda: registry.read_session(result.session_id) is None)

    page = transcripts.read_messages(result.session_id, limit=100)
    assert page.note is None
    assert [m.text for m in page.messages] == ["start\n/start", "cloning the repo now"]
    assert TRANSCRIPT_SECRET not in json.dumps(page.to_dict())


def test_a_crashed_session_s_transcript_is_scrubbed_too(config, monkeypatch):
    """Its entry is kept as the crash signal; its raw credentials are not."""
    monkeypatch.setenv("FAKE_LMER_TRANSCRIPT", SESSION_TRANSCRIPT)
    monkeypatch.setenv("FAKE_LMER_EXIT", "3")
    result = spawn.spawn_session(config, request_for())
    target = transcript_file(result.session_id)

    assert wait_for(
        lambda: target.is_file()
        and TRANSCRIPT_SECRET not in target.read_text(encoding="utf-8")
    ), "a crash must not leave a credential on disk"
    assert registry.read_session(result.session_id) is not None, (
        "the crash signal itself must still be there"
    )


def test_the_transcript_is_not_scrubbed_while_the_session_is_running(config, monkeypatch):
    """The harness holds the file open and appends to it.

    A rewrite in flight replaces the inode underneath that writer, so everything
    it appends afterwards is lost. The scrub is therefore tied to the process
    exiting — this asserts the negative first, then that a killed session (a
    crash, not a clean exit) still gets scrubbed.
    """
    monkeypatch.setenv("FAKE_LMER_TRANSCRIPT", SESSION_TRANSCRIPT)
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    target = transcript_file(result.session_id)
    try:
        assert wait_for(lambda: target.is_file())
        # Twice, with a beat in between: the first read could simply have won a
        # race, the second cannot while the child is still asleep.
        assert TRANSCRIPT_SECRET in target.read_text(encoding="utf-8")
        time.sleep(0.2)
        assert TRANSCRIPT_SECRET in target.read_text(encoding="utf-8")
    finally:
        os.kill(result.pid, 9)

    assert wait_for(
        lambda: TRANSCRIPT_SECRET not in target.read_text(encoding="utf-8")
    ), "a killed session's transcript still has to be scrubbed"


def test_a_session_whose_transcript_directory_cannot_be_created_still_starts(
    config, monkeypatch, tmp_path, caplog
):
    """Fail-soft: no transcript is the pre-T22 status quo, not a reason to refuse.

    The directory is made uncreatable by putting a regular file where its parent
    would be, which is an OSError from mkdir on any platform and needs no
    permission games.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        spawn, "transcript_dir_for", lambda sid: blocker / f"{sid}.transcript"
    )

    # Kept alive for the same reason as the other entry-exists assertions: the
    # watcher reaps on a clean exit, and the stub is fast enough to win that race
    # under load. See the "exit handling" section for which tool fits which case.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        assert registry.read_session(result.session_id) is not None
        assert not [
            spec for spec in mount_specs_in(result.command)
            if spec.split(":")[1:2] == [transcripts.CONTAINER_TRANSCRIPT_DIR]
        ], "a mount for a directory that does not exist would abort the launch"
    finally:
        os.kill(result.pid, 9)
    assert any("platform_transcript_dir_unusable" in r.message for r in caplog.records)


def test_scrubbing_a_session_that_wrote_no_transcript_is_not_fatal(config):
    """The stub writes nothing unless asked, so this is the plain spawn path."""
    result = spawn.spawn_session(config, request_for())
    assert wait_for(lambda: registry.read_session(result.session_id) is None)
    assert spawn.transcript_dir_for(result.session_id).is_dir()
    assert list(spawn.transcript_dir_for(result.session_id).rglob("*.jsonl")) == []


# --- the session's own log (#150) --------------------------------------------
#
# The host-side tee is written by a thread in this process holding the PTY master,
# an fd that dies with the daemon — so a restart used to cost every live session
# its scrollback (T36 recovers what it can over the control plane). A spawn now
# mounts a third per-session directory for the container's own supervisor to write
# its log into, and the read path prefers that file when it is there.
#
# What these tests are really about is the *when*. The writer ships in the session
# image and not in this checkout, so the default stub deliberately writes nothing:
# an older worker must keep behaving exactly as it did, forever, and the choice
# must be made by probing the file rather than by knowing anything about versions.

def test_the_container_log_directory_exists_before_the_session_starts(config):
    """The container mounts it; a directory created afterwards is too late."""
    result = spawn.spawn_session(config, request_for())
    directory = spawn.container_log_dir_for(result.session_id)
    assert directory.is_dir()
    assert directory.parent == store.logs_dir(), "beside the PTY log, by design"


def test_the_container_log_directory_is_private_to_this_user(config):
    """It holds every byte the session drew, and it is rw-mounted into a container."""
    result = spawn.spawn_session(config, request_for())
    mode = stat.S_IMODE(spawn.container_log_dir_for(result.session_id).stat().st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_the_container_log_mount_satisfies_lmer_s_own_validator(config):
    """Handed to the parser that will actually read it, as with the transcript.

    Asked of ``spawn``'s copy of the constant rather than the supervisor's: the
    suite redirects the writer's copy away from the live session log
    (``_isolate_session_log_dir``), and this test is about the destination the
    command being inspected was built from.
    """
    result = spawn.spawn_session(config, request_for())
    spec = mount_spec_for(result.command, spawn.CONTAINER_SESSION_LOG_DIR)
    specs = parse_dir_mount_specs([spec], "")
    assert len(specs) == 1
    assert specs[0].host == spawn.container_log_dir_for(result.session_id)
    assert specs[0].container == spawn.CONTAINER_SESSION_LOG_DIR
    assert specs[0].mode == "rw", "the supervisor appends to it all session long"


def test_an_older_image_writes_no_log_and_is_served_the_host_tee(config):
    """The mixed-fleet guarantee, end to end: nothing changes for an old worker.

    The stub is launched with the mount and ignores it, which is exactly what a
    session image without the in-container writer does — and the platform must then
    serve the tee, as it always did.
    """
    result = spawn.spawn_session(config, request_for())
    assert spawn.wait_for_exit_recorded(result.session_id)

    assert spawn.container_log_dir_for(result.session_id).is_dir()
    assert not spawn.container_log_path_for(result.session_id).exists()
    path, source = session_io.canonical_log(result.session_id)
    assert (path, source) == (
        spawn.log_path_for(result.session_id), session_io.LOG_SOURCE_HOST,
    )
    assert b"fake lmer started" in session_io.read_log(result.session_id).data


def test_a_newer_image_writes_its_own_log_and_that_is_what_is_served(
    config, monkeypatch
):
    """The upgrade path, through the real mount argument the stub was given.

    The stub finds the host directory by the mount's container destination — so
    this fails if the mount is malformed, aimed elsewhere, or absent, rather than
    asserting about a string spawn.py meant to build.
    """
    monkeypatch.setenv("FAKE_LMER_SESSION_LOG", "written from inside the container")
    result = spawn.spawn_session(config, request_for())
    assert spawn.wait_for_exit_recorded(result.session_id)

    logged = spawn.container_log_path_for(result.session_id)
    assert logged.is_file(), "the stub never found the session-log mount"
    path, source = session_io.canonical_log(result.session_id)
    assert (path, source) == (logged, session_io.LOG_SOURCE_CONTAINER)
    chunk = session_io.read_log(result.session_id)
    assert chunk.data == b"written from inside the container"
    assert b"fake lmer started" not in chunk.data, "one source per read, not both"


def test_the_session_s_own_log_outlives_a_clean_exit(config, monkeypatch):
    """It is the scrollback of a finished session, so the reap must not take it.

    The ports file beside it *is* removed on a clean exit; the two are checked
    together because that is the distinction the cleanup has to keep making.
    """
    monkeypatch.setenv("FAKE_LMER_SESSION_LOG", "still here afterwards")
    result = spawn.spawn_session(config, request_for())
    assert spawn.wait_for_exit_recorded(result.session_id)

    assert spawn.container_log_path_for(result.session_id).read_bytes() == (
        b"still here afterwards"
    )
    assert not spawn.ports_file_for(result.session_id).exists()


def test_a_session_whose_log_directory_cannot_be_created_still_starts(
    config, monkeypatch, tmp_path, caplog
):
    """Fail-soft: no in-container log is the status quo, not a reason to refuse.

    A session that cannot be started is worse than a session recorded only by the
    host-side tee — which is what every session was before this existed.
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        spawn, "container_log_dir_for", lambda sid: blocker / f"{sid}.session"
    )

    # Kept alive for the same reason as the other entry-exists assertions: the
    # watcher reaps on a clean exit and the stub is fast enough to win that race.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        assert registry.read_session(result.session_id) is not None
        assert not [
            spec for spec in mount_specs_in(result.command)
            if spec.split(":")[1:2] == [supervisor.CONTAINER_SESSION_LOG_DIR]
        ], "a mount for a directory that does not exist would abort the launch"
    finally:
        os.kill(result.pid, 9)
    assert any(
        "platform_container_log_dir_unusable" in r.message for r in caplog.records
    )


# --- crash detection with a lingering grandchild -----------------------------
#
# The bug this locks in: `kill -9` on a session whose grandchild still held the
# PTY slave open left the drain thread blocked, so nothing reaped the child. The
# zombie kept answering kill(pid, 0) and the fleet view reported it as `running`.

@pytest.fixture
def lingering_lmer(tmp_path):
    """A stub that leaves a background process holding the PTY open."""
    script = tmp_path / "lingering-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "lingering lmer started"\n'
        "sleep 120 &\n"          # inherits the pty; outlives the parent
        "sleep 120\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_sigkilled_session_is_detected_despite_a_grandchild_on_the_pty(
    platform_root, lingering_lmer
):
    config = cfg.load({"lmer_bin": str(lingering_lmer)})
    result = spawn.spawn_session(config, request_for())

    assert wait_for(
        lambda: registry.is_live(registry.read_session(result.session_id)),
        timeout=5.0,
    ), "session should start out live"

    os.kill(result.pid, 9)

    assert wait_for(
        lambda: not registry.is_live(registry.read_session(result.session_id)),
        timeout=5.0,
    ), "a SIGKILLed session must stop reading as live even with the PTY held open"

    entry = registry.read_session(result.session_id)
    assert entry is not None, "the entry must survive as the crash signal"


# --- transcript hijack ------------------------------------------------------
#
# --mount-dir stays permitted (mounting unrelated data into a session is
# legitimate), but last-wins means a caller aiming one at the container's
# transcript path would redirect the harness's JSONL somewhere the platform does
# not own: an empty chat view, and an UNSCRUBBED transcript outside the 0700 tree
# the scrub-on-exit knows about.

@pytest.mark.parametrize("hijack", [
    ("--mount-dir", "/tmp/mine:/home/developer/.claude/projects:rw"),
    ("--mount-dir=/tmp/mine:/home/developer/.claude/projects",),
    ("--mount-dir", "/tmp/mine:/home/developer/.claude/projects/:rw"),
])
def test_a_mount_aimed_at_the_transcript_destination_is_refused(config, hijack):
    with pytest.raises(spawn.SpawnError, match="may not target"):
        spawn.spawn_session(config, request_for(extra_args=hijack))


@pytest.mark.parametrize("hijack", [
    ("--mount-dir", f"/tmp/mine:{supervisor.CONTAINER_SESSION_LOG_DIR}:rw"),
    ("--mount-dir=/tmp/mine:" + supervisor.CONTAINER_SESSION_LOG_DIR,),
    ("--mount-dir", f"/tmp/mine:{supervisor.CONTAINER_SESSION_LOG_DIR}/:rw"),
])
def test_a_mount_aimed_at_the_session_log_destination_is_refused(config, hijack):
    """Last-wins would send the session's record somewhere nothing reads it back.

    Worse than losing it: the platform would keep serving the file at the path it
    owns, which the redirect leaves empty — a terminal view showing nothing for a
    session that is working fine.
    """
    with pytest.raises(spawn.SpawnError, match="may not target"):
        spawn.spawn_session(config, request_for(extra_args=hijack))


def test_an_unrelated_mount_dir_is_still_allowed(config):
    """The flag exists for a reason; only the one dishonest aim is refused."""
    result = spawn.spawn_session(
        config, request_for(extra_args=("--mount-dir", "/tmp/data:/data:ro"))
    )
    assert "--mount-dir" in result.command
    assert "/tmp/data:/data:ro" in result.command


def test_a_hijack_after_a_bare_double_dash_is_not_lmers_problem(config):
    """Everything after `--` is the container's command line, not lmer's."""
    result = spawn.spawn_session(
        config,
        request_for(extra_args=(
            "--", "echo", "--mount-dir", "/tmp/x:/home/developer/.claude/projects",
        )),
    )
    assert result.session_id


# --- the ask channel (T23) ---------------------------------------------------
#
# A spawned session gets a second per-session directory mounted in: the channel
# it posts questions to and reads the operator's answers from (spec D26). Two
# halves have to be true together — the mount, and the environment variable that
# tells the agent it has one — because either alone is a session that posts
# questions nobody reads or waits for answers nobody can write.

def test_the_ask_channel_exists_before_the_session_starts(config):
    result = spawn.spawn_session(config, request_for())
    directory = spawn.ask_dir_for(result.session_id)
    assert directory.is_dir()
    assert directory.parent == store.logs_dir(), "beside the PTY log, by design"


def test_the_ask_channel_is_private_to_this_user(config):
    result = spawn.spawn_session(config, request_for())
    mode = stat.S_IMODE(spawn.ask_dir_for(result.session_id).stat().st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_the_ask_mount_satisfies_lmer_s_own_validator(config):
    """Handed to the parser that will actually read it, as with the transcript."""
    result = spawn.spawn_session(config, request_for())
    spec = mount_spec_for(result.command, ask.CONTAINER_ASK_DIR)
    specs = parse_dir_mount_specs([spec], "")
    assert len(specs) == 1
    assert specs[0].host == spawn.ask_dir_for(result.session_id)
    assert specs[0].container == ask.CONTAINER_ASK_DIR
    assert specs[0].mode == "rw", "the session writes its questions into it"


def test_the_platform_mounts_do_not_collide(config):
    """All of them handed to the real validator together, which rejects a clash.

    Two mounts at one destination is the container runtime aborting the launch,
    and ``parse_dir_mount_specs`` is the check that catches it first — so they go
    through it as a set rather than being eyeballed here.
    """
    result = spawn.spawn_session(config, request_for())
    specs = parse_dir_mount_specs(mount_specs_in(result.command), "")
    assert len(specs) == len(PLATFORM_MOUNT_DESTINATIONS)
    assert sorted(spec.container for spec in specs) == sorted(
        PLATFORM_MOUNT_DESTINATIONS
    )
    assert all(spec.mode == "rw" for spec in specs)


def test_the_ask_mount_precedes_a_container_command(config):
    """After a bare `--` the flag is an argument to the container's command."""
    result = spawn.spawn_session(
        config, request_for(extra_args=("--", "echo", "hi"))
    )
    last_mount = max(
        index for index, arg in enumerate(result.command) if arg == "--mount-dir"
    )
    assert last_mount < result.command.index("--")


def test_the_child_sees_both_halves_of_the_channel(config):
    """The mount and the variable, as the spawned process actually got them.

    The stub locates the mount by its container destination and writes what
    ``LMER_ASK_DIR`` held into it — so this fails if either half is missing, and
    the file arriving at all is the mount working.
    """
    result = spawn.spawn_session(config, request_for())
    marker = spawn.ask_dir_for(result.session_id) / "child-saw-env"
    assert wait_for(marker.is_file), "the stub never found the ask mount"
    assert marker.read_text(encoding="utf-8") == ask.CONTAINER_ASK_DIR


def test_the_child_environment_carries_the_container_path(config, monkeypatch):
    """The value is the *container* path, not the host directory.

    The host path exists only on this side of the mount; handing it down would
    give the in-container CLI a directory that is not there — the "not
    orchestrated" exit, on a session that very much was.
    """
    captured = {}
    real_popen = spawn.subprocess.Popen

    def spy(command, **kwargs):
        captured.update(kwargs.get("env") or {})
        return real_popen(command, **kwargs)

    monkeypatch.setattr(spawn.subprocess, "Popen", spy)
    spawn.spawn_session(config, request_for())
    assert captured[ask.ASK_DIR_ENV] == ask.CONTAINER_ASK_DIR


def test_a_session_whose_channel_cannot_be_created_still_starts(
    config, monkeypatch, caplog
):
    """Fail-soft, and honest: no directory means no variable either.

    A session told it has a channel that was never mounted would post questions
    into nothing and block on answers that cannot arrive — strictly worse than
    having no channel at all, which ``lmer-ask`` reports as exit 3.
    """
    captured = {}
    real_popen = spawn.subprocess.Popen

    def spy(command, **kwargs):
        captured.update(kwargs.get("env") or {})
        return real_popen(command, **kwargs)

    monkeypatch.setattr(spawn.subprocess, "Popen", spy)
    monkeypatch.setattr(spawn.ask, "prepare_ask_dir", lambda sid: None)

    result = spawn.spawn_session(config, request_for())
    assert result.session_id
    assert ask.ASK_DIR_ENV not in captured
    assert not [
        spec for spec in mount_specs_in(result.command)
        if spec.split(":")[1:2] == [ask.CONTAINER_ASK_DIR]
    ]


def test_an_inherited_ask_dir_is_not_passed_through(config, monkeypatch):
    """A daemon started from inside a session must not hand its own channel on."""
    monkeypatch.setenv(ask.ASK_DIR_ENV, "/somebody/elses/channel")
    captured = {}
    real_popen = spawn.subprocess.Popen

    def spy(command, **kwargs):
        captured.update(kwargs.get("env") or {})
        return real_popen(command, **kwargs)

    monkeypatch.setattr(spawn.subprocess, "Popen", spy)
    monkeypatch.setattr(spawn.ask, "prepare_ask_dir", lambda sid: None)
    spawn.spawn_session(config, request_for())
    assert ask.ASK_DIR_ENV not in captured


@pytest.mark.parametrize("hijack", [
    ("--mount-dir", "/tmp/mine:/home/developer/.lmer-ask:rw"),
    ("--mount-dir=/tmp/mine:/home/developer/.lmer-ask",),
    ("--mount-dir", "/tmp/mine:/home/developer/.lmer-ask/:rw"),
])
def test_a_mount_aimed_at_the_ask_channel_is_refused(config, hijack):
    """Redirecting it would send the session's questions where nobody looks."""
    with pytest.raises(spawn.SpawnError, match="may not target"):
        spawn.spawn_session(config, request_for(extra_args=hijack))


def test_the_container_path_is_the_one_the_guard_protects():
    """The literal in the hijack parameters above, kept honest."""
    assert ask.CONTAINER_ASK_DIR == "/home/developer/.lmer-ask"


# --- the exit observation point ----------------------------------------------
#
# `spawn_session` returns while a thread is still waiting on the child, so
# "has this session's ending been recorded?" had no answer and callers inferred
# it by re-reading the registry until it changed. That inference is a race, and
# it is the one that produced three separate gate failures.

def test_the_ending_is_observable_rather_than_inferred(config):
    result = spawn.spawn_session(config, request_for())

    assert spawn.wait_for_exit_recorded(result.session_id, timeout=10), (
        "nothing signalled that the ending had been recorded"
    )
    # Everything the watcher does is done by the time it fires, not merely begun.
    assert registry.read_session(result.session_id) is None


def test_waiting_on_a_session_that_has_not_ended_times_out_falsely(config, monkeypatch):
    """The negative case, and the reason the return value is a bool.

    A caller must be able to tell "it ended" from "I gave up", because the two
    mean opposite things about the registry entry it is about to read.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        assert spawn.wait_for_exit_recorded(result.session_id, timeout=0.2) is False
        assert registry.read_session(result.session_id) is not None, (
            "the session is still running, so its entry must still be there"
        )
    finally:
        os.kill(result.pid, 9)


def test_a_crash_is_recorded_too_not_just_a_clean_exit(config, monkeypatch):
    """The event means "the ending was recorded", not "the entry went away".

    A crashed session deliberately KEEPS its entry as the crash signal, so an
    event that only fired on the clean path would leave every crash test back on
    polling — and those are the tests most likely to be waiting for something
    that never happens.
    """
    monkeypatch.setenv("FAKE_LMER_EXIT", "3")
    result = spawn.spawn_session(config, request_for())

    assert spawn.wait_for_exit_recorded(result.session_id)
    assert registry.read_session(result.session_id) is not None, (
        "the crash signal was reaped"
    )


def test_waiting_before_the_watcher_gets_there_still_works(config):
    """Whoever asks first creates the event.

    The waiter usually arrives before the watcher finishes, so an implementation
    where only the watcher creates it would hand the waiter a different object
    and block until the timeout — passing the negative test and hanging the
    positive one.
    """
    result = spawn.spawn_session(config, request_for())
    # No sleep: this races the watcher on purpose, which is the point.
    assert spawn.wait_for_exit_recorded(result.session_id, timeout=10)


def test_the_event_table_cannot_grow_without_bound(config):
    """A daemon spawns for its whole life; one Event per session is a leak."""
    assert spawn._EXIT_RECORDED_CAP > 0
    for index in range(spawn._EXIT_RECORDED_CAP + 25):
        spawn._exit_event(f"synthetic-{index}")

    assert len(spawn._EXIT_RECORDED) <= spawn._EXIT_RECORDED_CAP, (
        f"the table grew to {len(spawn._EXIT_RECORDED)} entries"
    )
    # The oldest go first: a caller waiting on an ending thousands of sessions
    # ago has already lost whatever race it cared about.
    assert "synthetic-0" not in spawn._EXIT_RECORDED
    assert f"synthetic-{spawn._EXIT_RECORDED_CAP + 24}" in spawn._EXIT_RECORDED
