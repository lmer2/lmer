"""The spawn form's added arguments, and the discovery behind its selects (T37).

``--preset``, ``--agents`` and — since T51 — ``--model`` are things an operator
picks in the run dialog, which makes them things that arrive over HTTP. They
travel as **typed fields** on :class:`lmer_platform.spawn.SpawnRequest` and are
refused in ``extra_args``, for the reason the rest of
:data:`lmer_platform.spawn._RESERVED_ARGS` exists: the platform emits each flag
itself and records what it emitted, and argparse is last-wins, so a second
spelling appended to argv does not clash with the platform's — it silently beats
it, leaving a registry entry that names a preset the session never applied.
Several tests here run the emitted argv back through ``lmer``'s own parser rather
than asserting on the list this code built, because the list this code built is
not the thing that has to be right.

The model is also the one launch fact a session reports back about *itself*,
through the file ``lmer`` already writes its published ports into: the daemon
cannot infer it (an exported ``LMER_LLM_NAME`` beats a preset's host-side, so a
guess would be wrong for exactly the runs that name a preset), so a run that
named no model still ends up saying what drove it — see the "what the session
reports back" section.

The selects need to know what the host has, hence ``GET /api/spawn-options``. The
property that matters most is what it does when it knows nothing: only ``chat``
ships in the code repo, ``LMER_TASKDEF_PATHS`` can add anything, and the work
repo's own tiers are read off a mirror that may be stale or absent (T73, the
section on it below) — so the host's list is *advisory* and routinely partial.
T37 answered that with three comboboxes; T51 reverses the
taskdef one to a ``v-select`` on the operator's reasoning that it is a static list,
and keeps the requirement met by the two things the markup tests below pin: the menu
always contains the current value, and a switch turns the field into plain text.
A dialog that cannot express a name the daemon would accept is strictly worse
than the plain text field it replaced, and it is this UI's only way in.

Partial is one thing; empty for a reason that is not the host's is another. The
"where the host looks for taskdefs at all" section covers the bug that made
discovery return nothing on an *installed* lmer — it asked install mode instead
of looking for the files — which left the select offering only the name the form
itself defaults to.

T88 adds a fourth field, ``title`` (with ``description`` beside it), and it is here
for the rule rather than for the dialog: it is not a form field at all — the
operator has the metadata tab — but it is the sharpest case of the same "typed
field, never argv" requirement, since ``lmer`` has no flag for it and argv it does
not recognise becomes the *container's command line*. It also has the one thing the
three flags above do not: a failure ordering, because the text is written after the
container is running and therefore can only ever cost the label.

That route also answers with the repository URL a blank field falls back to, so
what the form *prefills* is tested here too (T28, moved into this file in T48). It
is the same requirement in two halves as the selects: a default the backend offers
and the field ignores is no default, and a blank repo URL is the one field in this
dialog whose emptiness costs the run its identity.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_cli.cli import _record_published_ports, _record_session_model, parse_args
from lmer_cli.presets import resolve_agent_presets
from lmer_platform import api, meta, registry, runs, spawn, store
from lmer_platform import config as cfg
from tests.conftest import node_binary, require_node_toolchain, strip_lmer_env

SECRET = "test-secret-value"

WEB = Path(__file__).resolve().parent.parent / "web"
ADD_RUN = WEB / "src" / "components" / "AddRun.vue"
API_JS = WEB / "src" / "api.js"


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
    """A stub standing in for `lmer`: prints its argv, then exits.

    Deliberately not a re-implementation of the one in
    ``tests/test_platform_spawn.py`` — nothing here reads a mount, so the stub
    only has to be a real process with a real argv and a real exit.
    """
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        'exit "${FAKE_LMER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


TARGET = "https://gitlab.example.com/agents/global/-/work_items/141"


def request_for(**overrides):
    payload = {
        "taskdef": "develop",
        "target": TARGET,
        "repo_url": "https://gitlab.example.com/agents/global.git",
    }
    payload.update(overrides)
    return spawn.SpawnRequest(**payload)


def as_lmer_reads_it(command):
    """The child's argv back through ``lmer``'s own parser.

    The point of the exercise: the platform's job is not to build a list, it is
    to make a particular ``lmer`` invocation happen, and only the parser that
    invocation meets can say whether it did.
    """
    namespace, rest = parse_args(list(command[1:]))
    assert rest == [], f"lmer did not recognise {rest} in the emitted argv"
    return namespace


@pytest.fixture
def client(platform_root):
    return TestClient(api.create_app(cfg.load(), SECRET, state_builder=_no_state))


def _no_state(config, *, force_pull=False):
    """A canned fleet payload — nothing here needs a work repo."""
    return {"schema": 1, "runs": [], "attention": [], "counts": {},
            "totals": {"runs": 0, "live": 0, "attention": 0}}


def bearer():
    return {"Authorization": f"Bearer {SECRET}"}


# --- the fields actually reach the child ------------------------------------

def test_the_preset_and_agents_fields_reach_the_child_as_lmer_parses_them(config):
    namespace = as_lmer_reads_it(
        spawn.spawn_session(
            config, request_for(preset="sol-review", agents="fable,sol-review")
        ).command
    )

    assert namespace.preset == "sol-review"
    assert namespace.agents == "fable,sol-review"
    # And nothing was displaced doing it: the two positionals are still the ones
    # the run's identity was derived from.
    assert namespace.task == "develop"
    assert namespace.target == [TARGET]


def test_the_selection_is_one_token_lmer_splits_itself(config):
    """``--agents`` takes a comma-delimited string, not a repeated flag.

    Honouring the real spelling matters more than it looks: the child resolves
    every name against the host's presets file before the container starts, and
    a flag it does not recognise in that shape would fail the invocation rather
    than fan out.
    """
    command = spawn.spawn_session(config, request_for(agents="a,b,c")).command

    assert command.count("--agents") == 1
    assert as_lmer_reads_it(command).agents == "a,b,c"


def test_neither_field_is_spelled_when_it_was_not_asked_for(config):
    command = spawn.spawn_session(config, request_for()).command

    assert "--agents" not in command
    assert "--preset" not in command
    namespace = as_lmer_reads_it(command)
    assert namespace.agents is None and namespace.preset is None


def test_both_land_ahead_of_extra_args_and_its_container_command(config):
    """``extra_args`` may end in a bare ``--``; everything after it is the
    container's command line, so a platform flag emitted there would vanish."""
    command = spawn.spawn_session(
        config,
        request_for(preset="p", agents="a", extra_args=("--", "echo", "hi")),
    ).command

    assert command.index("--preset") < command.index("--")
    assert command.index("--agents") < command.index("--")


def test_the_entry_records_the_selection_the_child_was_given(config, monkeypatch):
    """The fleet view's copy of what is running has to be the copy that ran."""
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(
        config, request_for(preset="sol-review", agents="fable,sol-review")
    )

    entry = registry.read_session(result.session_id)
    assert entry["task"]["preset"] == "sol-review"
    assert entry["task"]["agents"] == "fable,sol-review"


# --- the model (T51) ---------------------------------------------------------
#
# The third field, and the one with two ways in: the operator names it here, or
# nobody does and the session reports what it resolved. Both land in the same
# place — ``task.model``, which is what RunView.model reads — so a row can say
# which model is driving a run without a client knowing which of the two happened.

def test_the_model_field_reaches_the_child_as_lmer_parses_it(config):
    namespace = as_lmer_reads_it(
        spawn.spawn_session(config, request_for(model="gpt-5.6-sol")).command
    )

    assert namespace.model == "gpt-5.6-sol"
    assert namespace.task == "develop"
    assert namespace.target == [TARGET]


def test_no_model_flag_is_spelled_when_none_was_asked_for(config):
    """Unset is not a value: it means the harness runs its own default, and a
    session told ``--model ''`` would be told something the operator did not."""
    command = spawn.spawn_session(config, request_for()).command

    assert "--model" not in command
    assert as_lmer_reads_it(command).model is None


def test_the_model_lands_ahead_of_extra_args_and_its_container_command(config):
    command = spawn.spawn_session(
        config, request_for(model="opus", extra_args=("--", "echo", "hi")),
    ).command

    assert command.index("--model") < command.index("--")


def test_the_entry_records_the_model_the_child_was_given(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for(model="opus"))
    try:
        entry = registry.read_session(result.session_id)
        assert entry["task"]["model"] == "opus"
    finally:
        os.kill(result.pid, 9)


def test_a_spawn_that_named_no_model_records_none_rather_than_a_guess(
    config, monkeypatch
):
    """The daemon's own ``LMER_LLM_NAME`` is not evidence about a run.

    It is applied to the child, which is why the child is the one that answers —
    but recording it here would put a model on the row of every session a preset
    started as something else, since ``lmer`` applies preset env only over keys
    the environment leaves unset.
    """
    monkeypatch.setenv("LMER_LLM_NAME", "the-daemons-own-model")
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = spawn.spawn_session(config, request_for())
    try:
        assert registry.read_session(result.session_id)["task"]["model"] is None
    finally:
        os.kill(result.pid, 9)


def test_the_smuggle_the_model_reservation_closes_is_real():
    """``lmer``'s own parser, on argv shaped exactly as a smuggle would shape it.

    Sharper than for the other two: the model also steers which harness runs when
    nothing names one, so the later spelling picks the agent CLI as well as the
    model, for a session whose entry says neither.
    """
    namespace, rest = parse_args([
        "develop", TARGET, "--fastapi", "--model", "recorded", "--model", "smuggled",
    ])

    assert rest == []
    assert namespace.model == "smuggled"


@pytest.mark.parametrize("smuggled", [
    "--model", "--model=opus", "--mode=opus", "--mod", "--mod=opus",
])
def test_a_model_in_extra_args_is_refused(config, smuggled):
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(config, request_for(extra_args=(smuggled, "opus")))
    assert registry.list_sessions(live_only=False) == []


def test_the_model_abbreviations_the_guard_refuses_are_ones_lmer_honours():
    """Otherwise the parametrisation above is a list of strings someone imagined.

    ``--mo`` is deliberately not among them: it is ambiguous with ``--mount-dir``
    and ``--mount-file``, so lmer refuses it on its own — the guard refuses it too
    (it is a prefix of ``--model``), and that costs nobody anything.
    """
    assert parse_args(["develop", TARGET, "--mod", "opus"])[0].model == "opus"
    assert parse_args(["develop", TARGET, "--mode", "opus"])[0].model == "opus"


def test_a_mount_dir_is_not_refused_by_the_model_reservation(config):
    """``--mount-dir`` shares a prefix with ``--model`` but is not one of its
    prefixes, and the platform's own transcript/ask mounts ride through here."""
    command = spawn.spawn_session(
        config, request_for(extra_args=("--mount-dir", "/tmp:/data:ro"))
    ).command

    assert "/tmp:/data:ro" in command


# --- why both are reserved in extra_args ------------------------------------

def test_the_smuggle_the_reservation_closes_is_real():
    """``lmer``'s own parser, on argv shaped exactly as a smuggle would shape it.

    The platform emits its flag from the typed field and ``extra_args`` lands
    after it, so the two do not collide — argparse takes the last one. Asserting
    that spawn.py refuses the flag would only restate spawn.py; this is the
    consequence that makes the refusal worth having.
    """
    namespace, rest = parse_args([
        "develop", TARGET, "--fastapi",
        "--preset", "recorded", "--agents", "recorded",
        "--preset", "smuggled", "--agents", "smuggled",
    ])

    assert rest == []
    assert (namespace.preset, namespace.agents) == ("smuggled", "smuggled"), (
        "the later spelling wins, so the session runs a configuration the "
        "platform's registry entry does not name"
    )


@pytest.mark.parametrize("smuggled", [
    "--agents", "--agents=fable", "--agent=fable", "--ag=fable", "--ag",
])
def test_an_agents_selection_in_extra_args_is_refused(config, smuggled):
    """Abbreviations included: ``--ag`` fans a session out exactly as well as the
    full spelling (verified against lmer's parser above), so a guard that only
    knew the full spelling would be decorative."""
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(config, request_for(extra_args=(smuggled, "fable")))
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("smuggled", [
    "--preset", "--preset=sol", "--prese=sol", "--pres", "--pre=sol",
])
def test_a_preset_in_extra_args_is_refused(config, smuggled):
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(config, request_for(extra_args=(smuggled, "sol")))
    assert registry.list_sessions(live_only=False) == []


def test_the_abbreviations_the_guard_refuses_are_ones_lmer_honours():
    """Otherwise the parametrisations above are a list of strings someone imagined."""
    assert parse_args(["develop", TARGET, "--ag", "fable"])[0].agents == "fable"
    assert parse_args(["develop", TARGET, "--pre", "sol"])[0].preset == "sol"


def test_a_reserved_selection_after_a_double_dash_is_the_container_s_business(config):
    """The scan stops at ``--``, and the two additions inherit that."""
    result = spawn.spawn_session(
        config, request_for(extra_args=("--", "echo", "--agents", "sol"))
    )
    assert result.command[-4:] == ["--", "echo", "--agents", "sol"]


def test_the_direction_flag_still_rides_through_beside_the_reserved_preset(config):
    """``--prompt`` merely *starts* like ``--preset``; only prefixes *of* a
    reserved flag are refused, and :mod:`lmer_platform.resume` depends on it —
    a direction is how a finished run is reopened at all."""
    command = spawn.spawn_session(
        config, request_for(extra_args=("--prompt=carry on",))
    ).command

    assert as_lmer_reads_it(command).prompt == "carry on"


# --- what an unusable value costs, and where it is caught -------------------

@pytest.mark.parametrize("field", ["preset", "agents", "model", "harness"])
@pytest.mark.parametrize("bad", ["", "   ", 5, ["fable"]])
def test_an_unusable_selection_is_refused(config, field, bad):
    with pytest.raises(spawn.SpawnError, match=f"{field} must be non-empty text"):
        spawn.spawn_session(config, request_for(**{field: bad}))


@pytest.mark.parametrize("field", ["preset", "agents", "model", "harness"])
def test_a_dash_leading_selection_is_refused(config, field):
    with pytest.raises(spawn.SpawnError, match="may not begin with a dash"):
        spawn.spawn_session(config, request_for(**{field: "--no-supervisor"}))


def test_a_non_string_harness_never_reaches_popen(config):
    """The typed field ``harness`` is the one flag argument that used to reach
    argv uncoerced: ``extra_args`` goes through ``str(arg)`` and this did not, so
    a number raised ``TypeError`` out of ``Popen`` — past the ``(OSError,
    ValueError)`` handler, as a 500 with the PTY's master fd leaked. Refused in
    ``validate`` now, which is before a port is drawn or a token minted."""
    with pytest.raises(spawn.SpawnError, match="harness must be non-empty text"):
        spawn.spawn_session(config, request_for(harness=5))
    assert registry.list_sessions(live_only=False) == []


def test_a_smuggled_harness_cannot_beat_the_recorded_one(config):
    """``extra_args`` lands last and argparse is last-wins, so a second
    ``--harness`` would run one agent CLI while the entry named another."""
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(
            config,
            request_for(harness="claude", extra_args=("--harness", "codex")),
        )
    assert registry.list_sessions(live_only=False) == []


def test_the_failure_the_dash_guard_prevents_is_real():
    """Both flags are two tokens, and argparse reads a dash-leading value as the
    next option rather than as the argument — the child exits 2 before it is a
    session, having looked accepted the whole way in."""
    with pytest.raises(SystemExit) as exited:
        parse_args(["develop", TARGET, "--preset", "--no-supervisor"])
    assert exited.value.code == 2


@pytest.mark.parametrize("bad", [",", " , ", ",,,"])
def test_an_agents_selection_that_names_nobody_is_refused(config, bad):
    with pytest.raises(spawn.SpawnError, match="names no agent"):
        spawn.spawn_session(config, request_for(agents=bad))


def test_lmer_itself_refuses_a_selection_that_names_nobody():
    """The reason the guard above is not pedantry: fanning out to nobody is an
    error in ``lmer``, not a quiet no-op, so the session would die at startup."""
    resolved, _warnings, error = resolve_agent_presets(",,", {})
    assert resolved is None
    assert error == "no agent names given"


def test_a_refused_selection_starts_nothing(config, monkeypatch):
    """The checks are in ``validate()``, which runs first: nothing is drawn,
    forked or written for a request that is refused."""
    def unreachable(*_args, **_kwargs):
        raise AssertionError("a refused request must not get this far")

    monkeypatch.setattr(spawn, "_pick_port", unreachable)
    monkeypatch.setattr(spawn.subprocess, "Popen", unreachable)

    with pytest.raises(spawn.SpawnError):
        spawn.spawn_session(config, request_for(agents="--smuggled"))

    assert registry.list_sessions(live_only=False) == []
    assert not list(store.sessions_dir().glob("*.token"))
    assert runs.list_tracked() == []


# --- naming the run at spawn time (T88) --------------------------------------
#
# The same "typed field, never argv" rule as the three above, arrived at from the
# opposite direction: ``lmer`` has no flag for a title, so there is nothing here to
# reserve and nothing to record in the registry entry — the text is this
# orchestrator's note about the run (:mod:`lmer_platform.meta`), written after the
# spawn is tracked. What that buys is the atomic path: an assistant spawning on the
# operator's behalf names the run in the same call, instead of a spawn and a
# rename that a crash in between leaves half-done.

def test_a_spawn_can_name_the_run_it_creates(config):
    """One call, and the run is in the fleet view under a name a human picked."""
    result = spawn.spawn_session(
        config, request_for(title="auth rate-limit fix", description="the **why**")
    )

    record = meta.read(result.host, result.project, result.slug)
    assert record.title == "auth rate-limit fix"
    assert record.description == "the **why**"
    assert result.warning is None


def test_the_title_is_stored_by_the_module_that_owns_that_text(config):
    """Not a copy of its rules: the same collapse and the same bound.

    A title is a label in a list, and it is the daemon that makes that true — so a
    spawn that hands over three lines must not put three lines on somebody's row.
    Asserting the *result* of ``meta``'s normalisation is what says this went
    through it rather than around it.
    """
    result = spawn.spawn_session(
        config, request_for(title="  a title\nwith  a newline\tin it  ")
    )

    assert meta.read(
        result.host, result.project, result.slug
    ).title == "a title with a newline in it"


def test_neither_field_is_ever_spelled_in_the_child_argv(config):
    """The whole reason they are fields: nothing about them is an invocation.

    Checked against ``lmer``'s own parser rather than the list this code built,
    because the list is not the thing that has to be right — and because the parser
    is where the consequence lives (see the test below).
    """
    title = "a title --with a dash in it"
    result = spawn.spawn_session(
        config, request_for(title=title, description="a description")
    )

    assert title not in result.command
    assert "--title" not in result.command and "--description" not in result.command
    assert not [arg for arg in result.command if "a description" in arg]
    namespace = as_lmer_reads_it(result.command)
    assert namespace.task == "develop"
    assert namespace.target == [TARGET]


def test_what_a_title_in_the_child_argv_would_actually_do():
    """Otherwise the test above is a rule with no consequence behind it.

    ``lmer`` has no ``--title``, and an unrecognised flag does not stop it: argv it
    does not know becomes the *container's command line* (``cmd_tokens = rest``), so
    a title emitted as a flag would replace the harness the session exists to run
    with an attempt to execute the label.
    """
    namespace, rest = parse_args([
        "develop", TARGET, "--fastapi", "--title", "auth rate-limit fix",
    ])

    assert namespace.task == "develop"
    assert rest == ["--title", "auth rate-limit fix"], (
        "lmer swallowed the flag instead of handing it to the container, which "
        "would make this a different argument than the one this test is about"
    )


def test_a_title_lmer_would_choke_on_is_still_only_a_label(config):
    """The flip side of not being a flag: no dash guard applies to it.

    ``preset``/``agents``/``model`` refuse a dash-leading value because argparse
    would read it as the next option and the child would exit 2. A title is never
    handed to argparse, so refusing the same shape here would be a rule imported
    from a place it does not apply — and an operator whose label starts with a dash
    would lose a session over punctuation.
    """
    result = spawn.spawn_session(config, request_for(title="--no-supervisor"))

    assert meta.read(
        result.host, result.project, result.slug
    ).title == "--no-supervisor"
    assert "--no-supervisor" not in result.command
    assert as_lmer_reads_it(result.command).no_supervisor is False


def test_the_title_is_not_copied_onto_the_tracked_index(config):
    """T52's storage decision, which a spawn-time title is the obvious place to
    undo: the index is what decides which runs exist, and a lost update there
    costs a *run* rather than a rename."""
    result = spawn.spawn_session(config, request_for(title="a name for the run"))

    index = json.loads(
        store.snapshot_path(runs.RUNS_FILE).read_text(encoding="utf-8")
    )
    assert "a name for the run" not in json.dumps(index)
    assert runs.get_tracked(result.host, result.project, result.slug) is not None
    assert (
        "a name for the run"
        in json.dumps(json.loads(
            store.snapshot_path(meta.META_FILE).read_text(encoding="utf-8")
        ))
    )


def test_a_run_with_no_identity_keeps_its_session_and_says_the_title_went_with_it(
    config
):
    """The failure ordering, at the one place it is reachable.

    A run nothing can be filed under is not a run metadata can attach to — and by
    the time that is known the container is running. So the title is dropped, the
    same warning that already reports the lost tracking says so too, and the
    session is left alone: a spawn killed over a label would be the worse outcome.
    """
    result = spawn.spawn_session(
        config, request_for(repo_url=None, target="feature/x", title="a lost name")
    )

    assert result.pid, "the session was not started"
    assert meta.load_all() == {}, "metadata was attached to an untracked run"
    assert "this run has no identity" in result.warning
    assert "were not stored" in result.warning
    assert "POST /api/runs/meta" in result.warning


def test_a_plain_repo_url_target_tracks_the_run_and_keeps_its_title(config):
    """A repository URL target is enough identity for atomic spawn naming."""
    target = "https://gitlab.example.com/agents/global"
    result = spawn.spawn_session(
        config,
        request_for(repo_url=None, target=target, title="cache handoff follow-up"),
    )

    tracked = runs.get_tracked(result.host, result.project, result.slug)
    assert tracked is not None
    assert tracked.repo == target
    assert meta.read(
        result.host, result.project, result.slug
    ).title == "cache handoff follow-up"
    assert result.warning is None


def test_a_web_page_target_remains_untracked_and_drops_its_title_loudly(config):
    """A URL-shaped page is not repository evidence for metadata attachment."""
    result = spawn.spawn_session(
        config,
        request_for(
            repo_url=None,
            target="https://gitlab.example.com/agents/global/-/pipelines/1691",
            title="must not attach to a fabricated project",
        ),
    )

    assert (result.host, result.project) == (None, None)
    assert runs.list_tracked() == []
    assert meta.load_all() == {}
    assert "not tracked" in result.warning
    assert "were not stored" in result.warning


def test_a_spawn_that_named_nothing_gets_no_warning_about_it(config):
    """The ordinary spawn, and the reason the sentence above is conditional: a
    warning that fires when nothing was asked for is one operators learn to skip."""
    result = spawn.spawn_session(config, request_for(repo_url=None, target="feature/x"))

    assert "this run has no identity" in result.warning
    assert "were not stored" not in result.warning


def test_a_session_with_no_repository_is_told_its_name_went_nowhere_too(config):
    """The designed case (spec D17) has no run either, and the note still cannot
    be attached — so the one thing it *is* worth warning about is the thing that
    was asked for and not done. Nothing else about this spawn warns."""
    result = spawn.spawn_session(
        config, request_for(repo_url=None, no_repo=True, title="a lost name")
    )

    assert meta.load_all() == {}
    assert "were not stored" in result.warning
    assert "this run has no identity" not in result.warning, (
        "the D17 case borrowed the untracked-run warning, which is the wording "
        "that was deliberately kept apart from it"
    )


def test_a_title_the_metadata_refuses_costs_the_label_and_not_the_session(config):
    """The other half of the ordering, and the one an over-eager agent will hit.

    The bound belongs to ``meta`` and is enforced there, which means the refusal
    arrives *after* the container started, the entry was written and the run was
    tracked. Raising then would report a failed spawn for a session that is
    working, with nothing to undo it with.
    """
    result = spawn.spawn_session(
        config, request_for(title="x" * (meta.MAX_TITLE_CHARS + 1))
    )

    assert result.pid
    assert runs.get_tracked(result.host, result.project, result.slug) is not None
    assert meta.read(result.host, result.project, result.slug).empty is True
    assert str(meta.MAX_TITLE_CHARS) in result.warning
    assert "was not stored" in result.warning


def test_a_snapshot_that_cannot_be_written_does_not_fail_the_spawn(
    config, monkeypatch
):
    """Same ordering, unhappier cause: the write is the last thing in a spawn and
    the least valuable thing in it, so a store failure is a warning too rather than
    a traceback out of a session that is already running."""
    def refuse(path, payload):
        raise store.StoreError("no space left on device")

    monkeypatch.setattr(meta, "write_json", refuse)

    result = spawn.spawn_session(config, request_for(title="a name for the run"))

    assert result.pid
    assert "no space left on device" in result.warning


# --- the HTTP surface --------------------------------------------------------

@pytest.fixture
def captured_spawn(monkeypatch):
    """Capture the request ``POST /api/sessions`` builds, without a spawn."""
    seen = {}

    def capture(config, request, kind="worker"):
        seen["request"] = request.validate()
        return spawn.SpawnResult(
            session_id="s-1", pid=4242, log_path=Path("/logs/s-1.log"),
            host="gitlab.example.com", project="agents/global", slug="develop-1",
            command=["lmer"], control_port=8123,
        )

    monkeypatch.setattr(api, "spawn_session", capture)
    return seen


def test_the_route_passes_all_three_as_typed_fields_and_not_as_argv(
    client, captured_spawn
):
    """The whole rule, at the place the request is assembled."""
    response = client.post(
        "/api/sessions", headers=bearer(),
        json={
            "taskdef": "develop", "target": TARGET,
            "preset": "sol-review", "agents": "fable,sol-review", "model": "opus",
        },
    )

    assert response.status_code == 201
    request = captured_spawn["request"]
    assert request.preset == "sol-review"
    assert request.agents == "fable,sol-review"
    assert request.model == "opus"
    assert request.extra_args == (), (
        "the three must not be appended to argv: extra_args is what a caller "
        "fills, and the platform's own copy would lose to it"
    )


def test_the_route_refuses_a_selection_smuggled_through_extra_args(client):
    response = client.post(
        "/api/sessions", headers=bearer(),
        json={
            "taskdef": "develop", "target": TARGET,
            "extra_args": ["--agents", "fable"],
        },
    )
    assert response.status_code == 400
    assert "not allowed in extra_args" in response.json()["detail"]


def test_the_route_refuses_an_unusable_selection_with_the_reason(client):
    response = client.post(
        "/api/sessions", headers=bearer(),
        json={"taskdef": "develop", "target": TARGET, "agents": "   "},
    )
    assert response.status_code == 400
    assert "agents must be non-empty text" in response.json()["detail"]


def test_the_route_refuses_a_model_smuggled_through_extra_args(client):
    response = client.post(
        "/api/sessions", headers=bearer(),
        json={
            "taskdef": "develop", "target": TARGET,
            "extra_args": ["--model", "opus"],
        },
    )
    assert response.status_code == 400
    assert "not allowed in extra_args" in response.json()["detail"]


def test_the_route_passes_the_title_and_description_as_typed_fields(
    client, captured_spawn
):
    """How an assistant names the run it is spawning: in the spawn body (T88)."""
    response = client.post(
        "/api/sessions", headers=bearer(),
        json={
            "taskdef": "develop", "target": TARGET,
            "title": "auth rate-limit fix", "description": "why this run exists",
        },
    )

    assert response.status_code == 201
    request = captured_spawn["request"]
    assert request.title == "auth rate-limit fix"
    assert request.description == "why this run exists"
    assert request.extra_args == (), (
        "the run's name is not part of the invocation: `lmer` has no flag for it, "
        "and argv it does not recognise becomes the container's command line"
    )


def test_a_spawn_that_names_no_run_leaves_both_fields_unset(client, captured_spawn):
    """``None`` and not ``""``: an empty string *clears* a field in ``meta``, so
    coercing an absent one would blank a description an operator wrote."""
    response = client.post(
        "/api/sessions", headers=bearer(),
        json={"taskdef": "develop", "target": TARGET},
    )

    assert response.status_code == 201
    assert captured_spawn["request"].title is None
    assert captured_spawn["request"].description is None


def test_the_route_list_shows_that_a_spawn_can_name_the_run(client):
    """The route list is what the assistant is told to believe over its taskdef
    (``GET /api`` is the authority), so a field only the taskdef mentions is one a
    rotation can lose."""
    body = client.get("/api", headers=bearer()).text

    sessions_route = body.split("POST /api/sessions")[1].split("GET  /api")[0]
    assert "title" in sessions_route
    assert "/api/runs/meta" in sessions_route, (
        "nothing connects the spawn field to the verb that sets the same thing "
        "afterwards"
    )


# --- what the session reports back (T51) -------------------------------------
#
# The other half of ``task.model``: a session that was told no model still knows
# which one it is running, and says so through the file it already writes its
# published ports into. The daemon folds that in on the read path, because the
# fact arrives after the entry was written — the same reason the ports do.

def _write_reported_facts(session_id, **facts):
    """Stand in for the session's own `lmer`, which writes this file at launch."""
    path = spawn.ports_file_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts), encoding="utf-8")
    return path


def test_the_two_sides_of_the_channel_agree(platform_root, monkeypatch):
    """The real writer into the real reader, because the contract between them is
    a string in a JSON file: ``lmer_cli`` names the key and this package looks it
    up, and the two live in different programs that are installed separately.
    Every other test here writes the file by hand, which cannot catch a rename.
    """
    registry.register("s-1", pid=os.getpid(), task={"taskdef": "develop"})
    monkeypatch.setenv(
        "LMER_PLATFORM_PORTS_FILE", str(spawn.ports_file_for("s-1"))
    )
    spawn.ports_file_for("s-1").parent.mkdir(parents=True, exist_ok=True)

    _record_session_model("gpt-5.6-sol")
    _record_published_ports([30021], "127.0.0.1")

    entry = spawn.absorb_ports(registry.list_sessions())[0]

    assert entry["task"]["model"] == "gpt-5.6-sol"
    assert entry["ports"] == [{"host": 30021, "container": 30021}]


def test_the_model_a_session_reports_lands_on_the_run(platform_root):
    """Which is what makes ``RunView.model`` answer at all: it reads this key."""
    registry.register(
        "s-1", pid=os.getpid(), task={"taskdef": "develop", "target": TARGET}
    )
    _write_reported_facts("s-1", model="claude-opus-5")

    absorbed = spawn.absorb_ports(registry.list_sessions())

    assert absorbed[0]["task"]["model"] == "claude-opus-5"
    assert registry.read_session("s-1")["task"]["model"] == "claude-opus-5", (
        "the fold has to persist, or every read re-does it"
    )


def test_absorbing_a_model_keeps_the_rest_of_the_task_block(platform_root):
    """``registry.update`` merges at the top level only, so a bare ``{model}``
    would take the taskdef and target the fleet view labels the row with."""
    registry.register(
        "s-1", pid=os.getpid(),
        task={"taskdef": "develop", "target": TARGET, "preset": "sol-review"},
    )
    _write_reported_facts("s-1", model="opus")

    task = spawn.absorb_ports(registry.list_sessions())[0]["task"]

    assert task["taskdef"] == "develop"
    assert task["target"] == TARGET
    assert task["preset"] == "sol-review"


def test_a_reported_model_never_overwrites_the_one_the_spawn_named(platform_root):
    """The request is the operator's statement of what to run; the report is the
    session's account of what it ran. They agree unless something went wrong, and
    when they disagree the one worth keeping is the one somebody can act on."""
    registry.register("s-1", pid=os.getpid(), task={"model": "opus"})
    _write_reported_facts("s-1", model="something-else")

    assert spawn.absorb_ports(registry.list_sessions())[0]["task"]["model"] == "opus"


def test_the_ports_and_the_model_arrive_in_one_file_and_both_land(platform_root):
    """`lmer` merges them into the one path the platform gave it, and neither
    write may cost the other — they happen at different points in a launch."""
    registry.register("s-1", pid=os.getpid(), task={"taskdef": "develop"})
    _write_reported_facts(
        "s-1", model="opus", ports=[{"host": 30021, "container": 30021}]
    )

    entry = spawn.absorb_ports(registry.list_sessions())[0]

    assert entry["ports"] == [{"host": 30021, "container": 30021}]
    assert entry["task"]["model"] == "opus"


@pytest.mark.parametrize("reported", [{}, {"model": None}, {"model": "  "},
                                      {"model": 5}, {"ports": []}])
def test_a_file_that_reports_no_usable_model_leaves_the_entry_alone(
    platform_root, reported
):
    """An older ``lmer`` writes no such key at all, and this is an interface
    between two independently-installed programs. ``None`` must not become the
    string "None" on somebody's row."""
    registry.register("s-1", pid=os.getpid(), task={"taskdef": "develop"})
    _write_reported_facts("s-1", **reported)

    assert spawn.absorb_ports(registry.list_sessions())[0]["task"].get("model") is None


def test_an_entry_with_a_task_block_of_the_wrong_shape_is_left_as_its_writer_left_it(
    platform_root
):
    """Same tolerance the inventory gives one: an entry from another version must
    not take the read path down with it — and must not be half-rewritten either,
    since the fleet view already reads a task block of that shape as no metadata.
    The ports still fold: those are a top-level key and owe the task block
    nothing."""
    registry.register("s-1", pid=os.getpid(), task="develop")
    _write_reported_facts(
        "s-1", model="opus", ports=[{"host": 30021, "container": 30021}]
    )

    entry = spawn.absorb_ports(registry.list_sessions())[0]

    assert entry["task"] == "develop"
    assert entry["ports"] == [{"host": 30021, "container": 30021}]


# --- what the host can offer the form ---------------------------------------

def plant_taskdefs(taskdef_dir, *names):
    """A ``taskdef/`` directory holding *names*, each with an instructions.txt."""
    for name in names:
        (taskdef_dir / name).mkdir(parents=True)
        (taskdef_dir / name / "instructions.txt").write_text(
            "do the thing", encoding="utf-8"
        )
    return taskdef_dir


def blind_the_host(monkeypatch, tmp_path):
    """Neutralise every route by which the host could find a ``taskdef/``.

    Needed because this repo *is* an lmer checkout, so several candidates resolve
    here for real: without this the assertions below would be about whatever
    happens to be committed in /workspace/taskdef. Each candidate is disabled the
    way an operator's host would lack it — no package-adjacent root, no repo root,
    and a working directory with nothing to do with lmer.
    """
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir(exist_ok=True)
    monkeypatch.setattr(api, "_package_taskdef_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(api, "repo_root_path", lambda: None)
    monkeypatch.chdir(nowhere)


@pytest.fixture
def taskdef_tree(tmp_path, monkeypatch):
    """A taskdef search path with two real taskdefs and one directory that isn't."""
    root = plant_taskdefs(tmp_path / "taskdefs", "chat", "moonshot")
    (root / "not-a-taskdef").mkdir()
    blind_the_host(monkeypatch, tmp_path)
    monkeypatch.setenv("LMER_TASKDEF_PATHS", str(root))
    return root


@pytest.fixture
def presets_file(tmp_path, monkeypatch):
    """A presets file whose bodies carry things that must never leave the host."""
    path = tmp_path / "presets.json"
    path.write_text(
        json.dumps({
            "sol-review": {
                "checkout": "/srv/private-checkout",
                "env": {"LMER_SECRET_TOKEN": "s3cr3t-value"},
            },
            "fable": {"args": ["--harness", "claude"]},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    return path


def test_spawn_options_lists_the_taskdefs_the_host_can_see(client, taskdef_tree):
    payload = client.get("/api/spawn-options", headers=bearer()).json()

    assert payload["taskdefs"] == ["chat", "moonshot"]
    assert "not-a-taskdef" not in payload["taskdefs"], (
        "a directory with no instructions.txt is not a taskdef, and offering it "
        "would be offering a spawn that fails"
    )


def test_the_assistants_taskdef_is_not_offered_on_the_run_form(
    client, tmp_path, monkeypatch
):
    """``orchestrate`` is spawned by the daemon as the assistant — no repo
    checkout, refuses to write code — so a *run* spawned with it cannot work
    (operator request). Excluded from the menu, not from the vocabulary: the
    free-text override still reaches lmer, which is the one that can settle it."""
    root = plant_taskdefs(
        tmp_path / "taskdefs", "chat", api.assistant.TASKDEF
    )
    blind_the_host(monkeypatch, tmp_path)
    monkeypatch.setenv("LMER_TASKDEF_PATHS", str(root))

    payload = client.get("/api/spawn-options", headers=bearer()).json()

    assert payload["taskdefs"] == ["chat"]


def test_spawn_options_lists_the_preset_names(client, presets_file):
    payload = client.get("/api/spawn-options", headers=bearer()).json()
    assert payload["presets"] == ["fable", "sol-review"]


def test_spawn_options_reports_preset_names_and_nothing_else(client, presets_file):
    """A preset body is host-side configuration: ``env`` holds credentials,
    ``checkout`` is a path on this machine. Only the name is ever selected by, so
    only the name goes out — the same rule ``_config_summary`` follows."""
    body = client.get("/api/spawn-options", headers=bearer()).text

    assert "s3cr3t-value" not in body
    assert "LMER_SECRET_TOKEN" not in body
    assert "/srv/private-checkout" not in body


def test_spawn_options_requires_auth(client):
    assert client.get("/api/spawn-options").status_code == 401


def test_the_route_list_mentions_the_discovery_route(client):
    body = client.get("/api", headers=bearer()).text
    assert "/api/spawn-options" in body


# --- the degradation requirement --------------------------------------------
#
# The single most important behaviour here. A host's taskdef list is advisory:
# only `chat` lives in the code repo, LMER_TASKDEF_PATHS can name anything, and a
# work-repo taskdef resolves inside the container where the daemon cannot see it.
# So discovery is routinely partial and may be empty — and the operator who knows
# the name they want must still be able to spawn.

def test_discovery_finding_nothing_never_costs_the_operator_the_form(monkeypatch):
    """Both halves of the enumeration can fail, and neither may raise.

    A 500 here, or an exception escaping into the route, replaces a working
    free-text form with an error — for a field whose value the operator already
    knew. Empty lists are the honest answer and the harmless one.
    """
    def unreadable(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(api, "_get_taskdef_paths", unreadable)
    monkeypatch.setattr(api, "load_presets", unreadable)

    payload = api.discover_spawn_options()

    assert payload["taskdefs"] == []
    assert payload["presets"] == []
    assert payload["advisory"] is True


def test_discovery_failing_is_still_a_200_with_the_empty_lists(client, monkeypatch):
    def unreadable(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(api, "_get_taskdef_paths", unreadable)
    monkeypatch.setattr(api, "load_presets", unreadable)

    response = client.get("/api/spawn-options", headers=bearer())

    assert response.status_code == 200
    assert response.json()["taskdefs"] == []


def test_a_host_with_no_taskdef_search_path_at_all_answers_empty(
    client, tmp_path, monkeypatch
):
    """An install with no checkout and no ``LMER_TASKDEF_PATHS``: ``lmer`` accepts
    any task name there and lets the container settle it, so the daemon must say
    the same."""
    blind_the_host(monkeypatch, tmp_path)
    payload = client.get("/api/spawn-options", headers=bearer()).json()
    assert payload["taskdefs"] == []
    assert payload["advisory"] is True


def test_a_taskdef_discovery_never_heard_of_still_spawns(config, monkeypatch):
    """Advisory all the way to the spawn: nothing checks a taskdef against the
    list, so the empty menu costs a completion and not a session."""
    monkeypatch.setattr(api, "repo_root_path", lambda: None)
    monkeypatch.setattr(api, "_get_taskdef_paths", lambda _root: [])
    assert api.discover_spawn_options()["taskdefs"] == []

    command = spawn.spawn_session(
        config, request_for(taskdef="a-taskdef-only-the-work-repo-has")
    ).command

    assert as_lmer_reads_it(command).task == "a-taskdef-only-the-work-repo-has"


# --- where the host looks for taskdefs at all --------------------------------
#
# The bug behind an empty menu, and the one the degradation requirement above was
# tolerating rather than preventing: discovery passed repo_root_path() to
# _get_taskdef_paths(), and that is None whenever install mode says INSTALLED. The
# paths list came back empty, discovery enumerated nothing, and the dialog offered
# only `develop` — the form's own default, which is not even a taskdef in this
# repo. Same root cause as the UI-not-found bug (e62ec99): gating on install mode
# instead of looking for the files. Install mode was never the question.
#
# A select over an empty list is what makes it more than cosmetic. T51 turned the
# taskdef field into a v-select on the reasoning that the list is static, so with
# discovery blind the operator's only way out is the pencil — which is why that
# escape hatch stays load-bearing whatever this section proves.

def test_taskdefs_are_found_when_install_mode_says_installed(monkeypatch):
    """The regression, in the shape the operator reported it: an lmer whose
    install mode says INSTALLED still enumerates the taskdefs beside it."""
    monkeypatch.setattr(api, "repo_root_path", lambda: None)
    monkeypatch.chdir("/tmp")

    assert "chat" in api._discovered_taskdefs(), (
        "the daemon can see this checkout's taskdef/ and must list it; an empty "
        "list leaves the dialog offering only its hardcoded default"
    )


def test_this_checkout_resolves_beside_the_package(monkeypatch):
    """Candidate 1: …/<root>/src/lmer_platform → …/<root>/taskdef, no env, no cwd."""
    monkeypatch.setattr(api, "repo_root_path", lambda: None)
    monkeypatch.chdir("/tmp")

    assert api._builtin_taskdef_root() == Path(api.__file__).resolve().parents[2]


def test_the_repo_root_is_a_candidate(tmp_path, monkeypatch):
    """Candidate 2: developer mode, with nothing beside the package."""
    root = tmp_path / "checkout"
    plant_taskdefs(root / "taskdef", "moonshot")
    monkeypatch.setattr(api, "_package_taskdef_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(api, "repo_root_path", lambda: root)
    monkeypatch.chdir(tmp_path)

    assert api._builtin_taskdef_root() == root
    assert api.discover_spawn_options()["taskdefs"] == ["moonshot"]


def test_the_working_directory_is_a_candidate(tmp_path, monkeypatch):
    """Candidate 3: a daemon started from a checkout it was not installed from."""
    root = tmp_path / "checkout"
    plant_taskdefs(root / "taskdef", "moonshot")
    monkeypatch.setattr(api, "_package_taskdef_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(api, "repo_root_path", lambda: None)
    monkeypatch.chdir(root)

    assert api._builtin_taskdef_root() == root
    assert api.discover_spawn_options()["taskdefs"] == ["moonshot"]


def test_the_package_root_wins_over_the_working_directory(tmp_path, monkeypatch):
    """Order matters where the two disagree: the taskdefs shipped with the lmer
    that is running are the ones it can actually resolve, so an unrelated checkout
    the daemon happens to have been started in must not shadow them."""
    installed = tmp_path / "installed"
    plant_taskdefs(installed / "taskdef", "chat")
    here = tmp_path / "some-other-checkout"
    plant_taskdefs(here / "taskdef", "moonshot")
    monkeypatch.setattr(api, "_package_taskdef_root", lambda: installed)
    monkeypatch.setattr(api, "repo_root_path", lambda: None)
    monkeypatch.chdir(here)

    assert api._builtin_taskdef_root() == installed
    assert api.discover_spawn_options()["taskdefs"] == ["chat"]


def test_a_directory_called_taskdef_with_no_taskdefs_in_it_is_skipped(
    tmp_path, monkeypatch
):
    """Someone else's ``taskdef/``, or an empty one, must not shadow a real one —
    otherwise the first candidate that merely exists costs the menu everything the
    next one had."""
    (tmp_path / "elsewhere" / "taskdef" / "notes").mkdir(parents=True)
    root = tmp_path / "checkout"
    plant_taskdefs(root / "taskdef", "moonshot")
    monkeypatch.setattr(api, "_package_taskdef_root", lambda: tmp_path / "elsewhere")
    monkeypatch.setattr(api, "repo_root_path", lambda: root)
    monkeypatch.chdir(tmp_path)

    assert api._builtin_taskdef_root() == root
    assert api.discover_spawn_options()["taskdefs"] == ["moonshot"]


def test_the_env_var_adds_to_the_built_in_taskdefs_rather_than_replacing_them(
    client, tmp_path, monkeypatch
):
    """``LMER_TASKDEF_PATHS`` is not in the candidate order because it is not a
    fallback: ``lmer`` searches it *beside* the built-in directory, and the menu
    has to say the same or an operator who added a directory would find the
    built-in ones gone."""
    root = tmp_path / "checkout"
    plant_taskdefs(root / "taskdef", "chat")
    monkeypatch.setattr(api, "_package_taskdef_root", lambda: root)
    monkeypatch.setattr(api, "repo_root_path", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "LMER_TASKDEF_PATHS", str(plant_taskdefs(tmp_path / "extra", "moonshot"))
    )

    assert client.get("/api/spawn-options", headers=bearer()).json()["taskdefs"] == [
        "chat", "moonshot",
    ]


# --- the work repo's own taskdefs, off this host's mirror (T73) ---------------
#
# The comment above (and the note this route used to return) said a work-repo
# taskdef "resolves inside the container and is invisible from out here". It was
# unwired, not invisible: the container searches {work}/{host}/{project}/taskdef
# then {work}/taskdef, and the daemon's mirror (spec D24) is a full checkout of
# that same repo. So the operator whose `develop` lives in the work repo was
# reaching for the pencil to type a name the daemon had on disk all along.
#
# Two tiers, and they are not the same problem. The global tier needs nothing but
# the mirror. The project tier is filed under the run's host and project, which
# is a property of the spawn being composed — hence the target- and repo-URL-aware
# lookup, and hence the requirement that it agree with what the spawn will do:
# both go through spawn._repo_urls + derive_run_identity, so a menu that offers a
# project tier is offering the tier that spawn will search.

@pytest.fixture
def mirror(platform_root):
    """The daemon's work-repo mirror, planted rather than cloned.

    Resolved through :attr:`PlatformConfig.mirror_path` rather than spelled out,
    so this keeps pointing at the directory the daemon actually reads.
    ``platform_root`` is what isolates it: the mirror is derived from the platform
    state dir, which that fixture has already moved into tmp_path — no test here
    touches a real clone, and nothing pulls.
    """
    root = cfg.load().mirror_path
    root.mkdir(parents=True)
    return root


def project_tier(mirror, host="gitlab.example.com", project="agents/global"):
    """The work repo's project-scoped taskdef dir, the way the container spells it."""
    return mirror / host / project / "taskdef"


def test_the_work_repos_global_taskdefs_are_listed(
    client, mirror, tmp_path, monkeypatch
):
    """``{mirror}/taskdef/`` — the tier that needs nothing but the mirror, and the
    one the operator's own taskdef most likely lives in."""
    plant_taskdefs(mirror / "taskdef", "develop")
    blind_the_host(monkeypatch, tmp_path)

    payload = client.get("/api/spawn-options", headers=bearer()).json()

    assert payload["taskdefs"] == ["develop"]


def test_the_work_repos_project_taskdefs_follow_the_target(
    client, mirror, tmp_path, monkeypatch
):
    """The project tier is only reachable once something names the project, and the
    target is what names it — this is ``lmer develop <MR-url>``'s own arrangement,
    where the repository comes out of the target and nothing else has to be said."""
    plant_taskdefs(project_tier(mirror), "project-only")
    blind_the_host(monkeypatch, tmp_path)

    listed = client.get(
        "/api/spawn-options", headers=bearer(), params={"target": TARGET}
    ).json()["taskdefs"]

    assert listed == ["project-only"]


def test_another_projects_taskdefs_are_not_offered_for_this_target(
    client, mirror, tmp_path, monkeypatch
):
    """Why the lookup is target-aware rather than a list of every project tier in
    the mirror: a taskdef the *container* will not find is a spawn that fails, and
    offering it is the same mistake as offering a directory with no
    instructions.txt."""
    plant_taskdefs(project_tier(mirror, project="other/project"), "someone-elses")
    blind_the_host(monkeypatch, tmp_path)

    listed = client.get(
        "/api/spawn-options", headers=bearer(), params={"target": TARGET}
    ).json()["taskdefs"]

    assert listed == []


def test_the_project_tier_follows_the_repo_url_when_the_target_names_no_project(
    client, mirror, tmp_path, monkeypatch
):
    """A target is routinely a branch name or a sentence. The repository field is
    the other half of the same question, and the spawn reads it first."""
    plant_taskdefs(project_tier(mirror), "project-only")
    blind_the_host(monkeypatch, tmp_path)

    listed = client.get(
        "/api/spawn-options",
        headers=bearer(),
        params={
            "target": "a-branch-name",
            "repo_url": "https://gitlab.example.com/agents/global.git",
        },
    ).json()["taskdefs"]

    assert listed == ["project-only"]


def test_with_nothing_named_the_project_tier_is_the_daemons_own_repository(
    client, mirror, tmp_path, monkeypatch
):
    """A form that has asked nothing yet still gets a truthful menu: a spawn naming
    no repository is filed under the daemon's own ``$LMER_REPO_URL``, so that is
    the tier such a spawn would search."""
    plant_taskdefs(project_tier(mirror), "project-only")
    blind_the_host(monkeypatch, tmp_path)
    monkeypatch.setenv("LMER_REPO_URL", "https://gitlab.example.com/agents/global.git")

    payload = client.get("/api/spawn-options", headers=bearer()).json()

    assert payload["taskdefs"] == ["project-only"]


def test_the_tiers_are_searched_in_the_order_the_container_searches_them(
    mirror, monkeypatch
):
    """Not a second opinion about the work repo's layout: the tiers and their order
    are asserted against the container's own resolver, which is the code that will
    settle every id this menu offers."""
    from lmer_cli.container import taskdefs as container_taskdefs

    plant_taskdefs(project_tier(mirror), "project-only")
    plant_taskdefs(mirror / "taskdef", "develop")
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(mirror))
    monkeypatch.setenv("LMER_REPO_HOST", "gitlab.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "agents/global")

    ours = api._work_repo_taskdef_dirs(cfg.load(), target=TARGET)

    assert [path.resolve() for path in ours] == [
        path.resolve() for path in container_taskdefs.work_repo_taskdef_dirs()
    ]


def test_a_taskdef_two_tiers_carry_is_offered_once(
    client, mirror, tmp_path, monkeypatch
):
    """One id is one taskdef: the session resolves it once, by the precedence
    above, and naming it twice would only ask the operator to wonder which of them
    they just picked."""
    plant_taskdefs(project_tier(mirror), "develop")
    plant_taskdefs(mirror / "taskdef", "develop")
    blind_the_host(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "LMER_TASKDEF_PATHS", str(plant_taskdefs(tmp_path / "extra", "develop"))
    )

    listed = client.get(
        "/api/spawn-options", headers=bearer(), params={"target": TARGET}
    ).json()["taskdefs"]

    assert listed == ["develop"]


def test_the_assistants_taskdef_is_not_offered_from_the_work_repo_either(
    client, mirror, tmp_path, monkeypatch
):
    """The exclusion is about what the daemon does with that taskdef, not about
    where the file came from — and a work repo is free to carry its own copy."""
    plant_taskdefs(project_tier(mirror), api.assistant.TASKDEF, "project-only")
    plant_taskdefs(mirror / "taskdef", api.assistant.TASKDEF)
    blind_the_host(monkeypatch, tmp_path)

    listed = client.get(
        "/api/spawn-options", headers=bearer(), params={"target": TARGET}
    ).json()["taskdefs"]

    assert listed == ["project-only"]


def test_a_host_with_no_mirror_lists_exactly_what_it_listed_before(
    client, taskdef_tree, platform_root
):
    """The degradation that makes this safe to add at all: no mirror cloned is the
    ordinary state of a fresh daemon, and it must cost the menu nothing it had."""
    assert not cfg.load().mirror_path.exists()

    payload = client.get(
        "/api/spawn-options", headers=bearer(), params={"target": TARGET}
    ).json()

    assert payload["taskdefs"] == ["chat", "moonshot"]


def test_a_mirror_that_cannot_be_read_costs_the_menu_only_the_work_repo(
    client, mirror, taskdef_tree, monkeypatch
):
    """Scoped tighter than the outer catch on purpose: a mirror the daemon trips
    over must not take the tiers it *can* read down with it."""
    plant_taskdefs(mirror / "taskdef", "develop")

    def unreadable(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(api, "derive_run_identity", unreadable)

    payload = client.get(
        "/api/spawn-options", headers=bearer(), params={"target": TARGET}
    ).json()

    assert payload["taskdefs"] == ["chat", "moonshot"]
    assert payload["advisory"] is True


def test_the_menu_reads_the_mirror_as_it_stands_and_never_pulls(
    client, mirror, tmp_path, monkeypatch
):
    """Staleness is the accepted cost (spec D24): the fleet poll keeps the mirror
    current, and a select is no reason to put a ``git fetch`` on a request path —
    the same call ``GET /api/runs/files`` declines to make."""
    plant_taskdefs(mirror / "taskdef", "develop")
    blind_the_host(monkeypatch, tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the spawn form pulled the mirror")

    monkeypatch.setattr(api, "pull", refuse)

    payload = client.get("/api/spawn-options", headers=bearer()).json()

    assert payload["taskdefs"] == ["develop"]


def test_a_repo_url_cannot_walk_the_menu_out_of_the_mirror(
    client, mirror, tmp_path, monkeypatch
):
    """``_parse_repo_url`` hands back whatever path the URL had, ``..`` included,
    and the project tier is built from it — so the same containment re-check
    ``_safe_asset`` makes applies here. Without it a crafted repository URL turns
    the menu into a directory listing of this host."""
    escaped = plant_taskdefs(mirror.parent / "elsewhere" / "taskdef", "escaped")
    assert escaped.is_dir(), "the escape target has to exist for this to prove anything"
    blind_the_host(monkeypatch, tmp_path)

    listed = client.get(
        "/api/spawn-options",
        headers=bearer(),
        params={"repo_url": "https://gitlab.example.com/../../elsewhere"},
    ).json()["taskdefs"]

    assert listed == []


def test_a_preset_discovery_never_heard_of_still_spawns(config, monkeypatch):
    """The same for presets, which the daemon can only see through
    ``LMER_PRESETS_FILE`` — a file the operator may keep somewhere the daemon's
    environment does not name."""
    monkeypatch.setattr(api, "load_presets", dict)
    assert api.discover_spawn_options()["presets"] == []

    command = spawn.spawn_session(config, request_for(preset="not-in-the-file")).command

    assert as_lmer_reads_it(command).preset == "not-in-the-file"


# --- the URL a blank repo field falls back to (T28) --------------------------
#
# The blank field is half the bug T28 fixed: the daemon had a repository URL
# exported and the form showed an empty box beside it. This route therefore carries
# the value the spawn path would itself have used, so the prefill and the fallback
# cannot disagree — and it is scrubbed, because an exported LMER_REPO_URL routinely
# carries a token and this one lands in the DOM.

#: A token in the shape a host token has: ``lmer`` bakes one into the URL it hands
#: the container, so a daemon started from inside a session has a tokenised
#: LMER_REPO_URL as a matter of course.
HOST_TOKEN = "glpat-not-a-real-token-000"


def test_the_form_is_offered_the_url_a_blank_field_would_fall_back_to(
    platform_root, monkeypatch
):
    monkeypatch.setenv("LMER_REPO_URL", "https://gitlab.example.com/agents/global.git")

    offered = api.discover_spawn_options()["repo_url"]

    assert offered == "https://gitlab.example.com/agents/global.git"
    assert offered == spawn._repo_urls(request_for(repo_url=None))[0], (
        "the field would be prefilled with something other than the default it "
        "stands in for"
    )


def test_a_daemon_with_no_repo_url_offers_none(platform_root):
    """The case where leaving the field blank costs the run its identity, so the
    form must not be able to show anything reassuring."""
    assert api.discover_spawn_options()["repo_url"] is None


def test_the_offered_url_carries_no_credential(platform_root, monkeypatch):
    """It goes into an HTTP response and from there into the DOM.

    ``lmer`` bakes a host token into the URL it hands the container, so a daemon
    started from inside a session has a tokenised LMER_REPO_URL — the same reason
    ``_config_summary`` takes the work repo URL from the scrubbed mirror status.
    """
    monkeypatch.setenv(
        "LMER_REPO_URL",
        f"https://oauth2:{HOST_TOKEN}@gitlab.example.com/agents/global.git",
    )

    payload = api.discover_spawn_options()

    assert HOST_TOKEN not in json.dumps(payload)
    assert payload["repo_url"] == "https://gitlab.example.com/agents/global.git"


def test_the_credential_is_gone_from_what_the_spawn_records_too(config, monkeypatch):
    """The fallback and the prefill are one value, so the scrub is one scrub.

    Recording the tokenised spelling would put a live token in the run index and in
    every session entry filed under that run — files that get pasted into tickets.
    """
    monkeypatch.setenv(
        "LMER_REPO_URL",
        f"https://oauth2:{HOST_TOKEN}@gitlab.example.com/agents/global.git",
    )
    result = spawn.spawn_session(config, request_for(repo_url=None))

    tracked = runs.get_tracked(result.host, result.project, result.slug)
    assert tracked.repo == "https://gitlab.example.com/agents/global.git"
    assert HOST_TOKEN not in json.dumps([e.to_dict() for e in runs.list_tracked()])


# --- the same requirement, in the markup ------------------------------------
#
# There is deliberately no JS test runner in this repo (see
# tests/test_platform_web_app.py), so this is a source-level invariant. It is
# still the load-bearing half of the requirement: the backend degrading correctly
# is worth nothing if the field it feeds is a locked menu.

def _add_run_source():
    return ADD_RUN.read_text(encoding="utf-8")


def _field_elements(text, model):
    """The opening tags of every element bound to *model*, tag name first."""
    found = []
    at = text.find(f'v-model="{model}"')
    while at != -1:
        found.append(text[text.rindex("<", 0, at) + 1:text.index(">", at)])
        at = text.find(f'v-model="{model}"', at + 1)
    return found


def _field_element(text, model):
    """The opening tag of the one element bound to *model*."""
    elements = _field_elements(text, model)
    assert len(elements) == 1, f"{len(elements)} elements are bound to {model}"
    return elements[0]


def test_the_preset_and_agent_name_fields_stay_free_text():
    """A v-select can only submit a value the host enumerated, and the host
    cannot enumerate the presets an operator keeps in a file its environment does
    not name — ``LMER_PRESETS_FILE`` is read where ``lmer`` runs, not where the
    daemon does. Unlike the taskdef list, this one is not static either: it is
    whatever that file says today.
    """
    text = _add_run_source()

    for model in ("spawn.preset", "spawn.agents"):
        element = _field_element(text, model)
        tag = element.split()[0]
        assert tag == "v-combobox", (
            f"{model} is a <{tag}>: only a combobox accepts a name the host "
            "could not enumerate"
        )
        assert ":items=" in element, f"{model} offers no suggestions at all"
        for locked in ("readonly", "disabled"):
            assert locked not in element, (
                f"{model} is {locked}, which is the locked menu by another route"
            )


def test_the_taskdef_menu_can_still_express_a_name_the_host_cannot_see():
    """T51 reverses T37's combobox here, and this is the price of the reversal.

    A v-select submits only what the host enumerated, and the host's taskdef list
    is routinely partial — a work-repo taskdef resolves inside the container,
    where the daemon cannot see it. Two things keep that from costing the
    operator the session, and both are load-bearing rather than decorative: the
    menu carries the current value even when discovery never mentioned it, and
    the field switches to plain text on request. Take either away and this dialog
    — the UI's only way to start a session — can no longer name every taskdef the
    daemon would accept.
    """
    text = _add_run_source()
    elements = _field_elements(text, "spawn.taskdef")
    tags = [element.split()[0] for element in elements]

    assert tags == ["v-select", "v-text-field"], (
        f"the taskdef field is {tags}: a menu with no typeable alternative traps "
        "the operator in what the daemon happened to enumerate"
    )
    assert ":items=\"taskdefItems\"" in elements[0], (
        "the menu is fed the raw discovery list, so a default the host does not "
        "list is a default that cannot be submitted"
    )
    assert "!listed.includes(current)" in text, (
        "taskdefItems drops the current value when discovery does not list it"
    )
    assert 'click:append="taskdefFreeText = true"' in elements[0], (
        "there is no way out of the menu"
    )
    assert 'click:append="taskdefFreeText = false"' in elements[1]
    for element in elements:
        for locked in ("readonly", "disabled"):
            assert locked not in element, (
                f"the taskdef field is {locked}, which is the locked menu by "
                "another route"
            )


#: The only fields that may be a menu, and the rule is the reason rather than the
#: count: a ``v-select`` submits only what the host enumerated, so it is allowed
#: exactly where the host's list is the complete one and a value outside it could
#: not work anyway.
#:
#: - ``taskdef`` — the operator's call, on the reasoning that the list is static.
#: - ``slot`` — service slots are declared in the *daemon's own* ``config.json``
#:   and validated by the same process that serves this list, so unlike the
#:   presets file (read where ``lmer`` runs, not where the daemon does) there is
#:   no second place a name could come from. A slot the daemon does not declare is
#:   refused with a 400, which makes free text here a field whose only useful
#:   values are the enumerated ones.
#:
#: Every other name field stays free text — see
#: :func:`test_the_preset_and_agent_name_fields_stay_free_text`.
MENU_FIELDS = {"spawn.taskdef", "spawn.slot"}


def test_only_the_fields_the_host_can_enumerate_are_menus():
    """The reversal is scoped: a name field the host has no complete list for
    must not be a menu, because that is a field with no way out."""
    text = _add_run_source()

    menus = re.findall(r'<v-select\b[^>]*?v-model="([^"]+)"', text, re.S)
    assert set(menus) <= MENU_FIELDS, (
        f"{sorted(set(menus) - MENU_FIELDS)} is a menu, but the host has no "
        "complete list for it"
    )
    assert text.count("<v-select") == len(menus), (
        "a v-select is bound to no field at all"
    )
    assert _field_elements(text, "spawn.taskdef")[0].split()[0] == "v-select", (
        "the taskdef field is no longer the menu the operator asked for"
    )


def test_the_default_taskdef_survives_discovery():
    """It is offered as the default and overridden by typing — the behaviour that
    was already there. Discovery adds suggestions beside it and must never
    assign over it, least of all with a list that does not contain it."""
    text = _add_run_source()

    assert "taskdef: 'develop'" in text
    assert not re.search(r"spawn\.value\.taskdef\s*=", text), (
        "something assigns to the taskdef after the form was built"
    )


def test_a_failed_discovery_is_not_shown_as_a_form_error():
    """The fields work without it, so reporting it would only tell an operator
    who knows exactly what they want that the dialog is broken."""
    text = _add_run_source()
    body = text[text.index("async function loadOptions"):]
    body = body[:body.index("\n}\n")]

    assert "catch" in body
    assert "error.value" not in body, "a failed lookup is presented as a failure"


def test_the_menu_asks_again_when_the_target_or_the_repository_changes():
    """The work repo's project taskdefs are filed under the run's host and project,
    so the daemon can only offer that tier for a spawn it has been told about. A
    dialog that asked once, before either field was filled in, would show a menu
    for the daemon's default repository and quietly omit the tier the operator came
    here for — the failure this whole feature is about, one layer up."""
    text = _add_run_source()

    assert "fetchSpawnOptions({ target, repoUrl })" in text, (
        "the lookup does not say which spawn it is asking about"
    )
    watched = text[text.index("watch("):]
    watched = watched[:watched.index("\n)\n")]
    for field in ("spawn.value.target", "spawn.value.repo_url"):
        assert field in watched, f"a change to {field} does not refresh the menu"
    assert "loadOptions" in watched


def test_the_lookup_spells_the_two_arguments_the_way_the_route_reads_them():
    """The one place a camelCase/snake_case slip would be invisible: the request
    would still succeed, the route would still answer 200, and the project tier
    would simply never appear — indistinguishable from a mirror that does not have
    it."""
    source = API_JS.read_text(encoding="utf-8")

    assert "query.set('target', target)" in source
    assert "query.set('repo_url', repoUrl)" in source, (
        "the route reads repo_url; a repoUrl parameter is silently ignored"
    )


def test_the_refresh_is_debounced():
    """Both watched fields are typed into, and each keystroke would otherwise ask
    the daemon to walk its mirror for a URL nobody has finished pasting."""
    text = _add_run_source()

    assert "setTimeout(loadOptions, OPTIONS_REFRESH_DELAY_MS)" in text
    assert "clearTimeout(optionsTimer)" in text


def test_the_agents_selection_is_sent_in_the_shape_lmer_takes():
    """A multi-select hands back an array; ``lmer --agents`` takes one
    comma-delimited string and splits it itself."""
    text = _add_run_source()
    assert "join(','" in text, "the agents chips are not joined into one value"
    assert "payload.agents = agentsSelection.value" in text


def test_no_selection_is_sent_empty():
    """The daemon refuses a present-but-blank value, which is right — but an
    untouched field is not a value, so it must not be sent at all."""
    text = _add_run_source()
    assert "if (typed(spawn.value.preset)) payload.preset" in text
    assert "if (agentsSelection.value) payload.agents" in text
    assert "if (typed(spawn.value.model)) payload.model" in text


def test_the_model_field_is_typeable_and_offers_no_menu():
    """The one name field the host cannot even partially enumerate: which models
    a harness serves is the harness's business, and it says so by rejecting what
    it does not know. A list here would be a list of guesses."""
    text = _add_run_source()
    element = _field_element(text, "spawn.model")

    assert element.split()[0] == "v-text-field"
    assert ":items=" not in element
    assert "model: ''" in text, "the field has no initial value to bind"


# The repo URL field, same rule: the backend offering the default above is worth
# nothing if the field ignores it, the hint is the only place an operator learns
# what blank costs before finding out, and the warning is only loud if the dialog
# repeats it.

def test_the_repo_url_field_is_prefilled_from_the_discovered_default():
    text = _add_run_source()

    assert "payload.repo_url && !typed(spawn.value.repo_url)" in text, (
        "the discovered default is either not applied, or applied over a value the "
        "operator had already typed"
    )
    assert "spawn.value.repo_url = payload.repo_url" in text


def _repo_url_hint():
    """The hint constant's literal text, continuation lines included."""
    text = _add_run_source()
    lines = text[text.index("const REPO_URL_HINT"):].splitlines()
    collected = [lines[0]]
    for line in lines[1:]:
        if not line.strip().startswith("+"):
            break
        collected.append(line)
    return " ".join(collected)


def test_the_repo_url_hint_states_what_leaving_it_blank_costs():
    """It used to say the field falls back to $LMER_REPO_URL and stop there, which
    reads as "optional" — and a field that quietly loses the run is not optional."""
    hint = _repo_url_hint()

    assert "not tracked" in hint
    assert "disappears" in hint
    assert ':hint="REPO_URL_HINT"' in _add_run_source(), (
        "the field the hint is about does not show it"
    )


def test_a_spawn_that_lost_its_run_is_shown_that_in_the_dialog():
    """The daemon's warning is only loud if the UI repeats it."""
    text = _add_run_source()

    assert "warning.value = result.warning" in text
    assert 'v-alert v-if="warning" type="warning"' in text


# --- executed check ----------------------------------------------------------

COMPILE_AND_REPORT = """
import { parse, compileTemplate } from '@vue/compiler-sfc'
import { readFileSync } from 'node:fs'

const file = process.argv[1]
const { descriptor, errors } = parse(readFileSync(file, 'utf8'), { filename: file })
if (errors.length) { console.error(String(errors[0])); process.exit(1) }
const compiled = compileTemplate({
  source: descriptor.template.content, id: 'add-run', filename: file,
})
if (compiled.errors.length) { console.error(String(compiled.errors[0])); process.exit(1) }
const used = [...compiled.code.matchAll(/resolveComponent\\("([^"]+)"\\)/g)]
console.log(JSON.stringify([...new Set(used.map((m) => m[1]))].sort()))
"""


def test_the_template_compiles_and_resolves_both_field_components():
    """The source assertions above read strings; this reads the compiled render
    function, so a template that does not compile — or a tag Vue resolves to
    something other than the component named — fails here rather than on a phone.

    Both components have to be there now: the taskdef menu (T51) and the
    comboboxes the preset and agent fields still are.

    Missing Node skips, unless the host says it has one
    (:func:`tests.conftest.require_node_toolchain`), which is why the source
    assertions above and not this one are the *named* guard for the degradation
    requirement: a machine without a toolchain still has to be told when the
    template stops naming both components.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    result = subprocess.run(
        [node, "--input-type=module", "-e", COMPILE_AND_REPORT, str(ADD_RUN)],
        capture_output=True, text=True, timeout=60, cwd=str(WEB),
    )

    assert result.returncode == 0, result.stderr or result.stdout
    components = json.loads(result.stdout)
    assert "v-combobox" in components
    assert "v-select" in components
