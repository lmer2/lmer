"""Tests for continuing a tracked run (issue #141, slice M2 / T25).

Resume is "spawn this run's next session", so the tests that matter are about
*which run the session lands in* and about every case where the platform must
refuse instead of starting a container that leaves the operator no better off.

Sessions are spawned for real against a stub standing in for ``lmer`` — the same
approach as tests/test_platform_answer.py, with the stub recording its own
``sys.argv`` and running it through ``lmer``'s own parser. Argv is a list and not
a shell string, so that is what proves a target full of metacharacters (or a
direction that looks like a flag) arrives intact rather than proving this module
built a string it liked.

Five properties get the most attention:

- **continue vs sibling**: the recorded taskdef continues the run in front of the
  operator; an overridden one starts a different run against the same target, and
  the reply says which happened;
- **which session is continued**: in schema form a slug groups several complete,
  independent records (D22), so a resume says which one it read — by naming the
  session dir it found, in the reply and in every refusal — and a caller that
  means a particular launch names it with ``session_id``;
- **the landing run is what gets checked**: a ``complete`` ``develop`` run is the
  normal starting point for a ``review``, so the status that matters is the one of
  the run the session will actually file itself under;
- **nothing is guessed**: a missing repo URL and a finished run are asked about,
  with a machine-recognisable code, rather than papered over;
- **the platform writes nothing to run state** (spec D3) — the mirror is
  byte-for-byte untouched by a resume.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_platform import config as cfg
from lmer_platform import (
    api, mute, registry, resume as resume_mod, runs, spawn, store, workrepo,
)
from tests.conftest import strip_lmer_env
from work_repo import run_state

HOST = "gitlab.example.com"
PROJECT = "agents/global"
TASKDEF = "develop"
TARGET = "https://gitlab.example.com/agents/global/-/work_items/141"
REPO = "https://gitlab.example.com/agents/global"
QUESTION = "Should the queue survive a daemon restart, or start empty?"
#: Session ids in the schema-form grammar (``LMER_SESSION_ID``): timestamp-first,
#: so they sort into start order and the last one is the newest launch — the rule
#: ``workrepo._sessions_in`` holds for the picker and the resolver alike.
SESSION_ID = "20260824-165230-2-31245"
LATER_SESSION_ID = "20260825-090000-2-31300"


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
def resume_recording_lmer(tmp_path):
    """A stub that records its argv *and* what ``lmer``'s parser makes of it.

    Both halves are needed, for the reason test_platform_answer.py's stub needs
    them: the argv proves the platform passed one ``--prompt=`` token rather than
    two arguments (the two-token spelling loses any direction starting with a
    dash — argparse reads the value as an option), and the parsed values prove the
    taskdef, target and direction ``lmer`` recovers are the ones the operator
    asked for. That contract spans two packages, so asserting on this module's
    intentions alone would not test it.

    ``session`` is the third value of that kind and the newest (issue #353): the
    record a continue lands in is decided by the ``LMER_SESSION_ID`` this flag
    sets, so what matters is that ``lmer``'s parser takes the token as its own
    rather than leaving it in ``rest``.
    """
    dump = tmp_path / "resume.json"
    script = tmp_path / "resume-lmer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from lmer_cli.cli import parse_args\n"
        "ns, rest = parse_args(sys.argv[1:])\n"
        "payload = {'argv': sys.argv[1:], 'task': ns.task, 'target': ns.target,\n"
        "           'prompt': ns.prompt, 'answer': ns.answer, 'rest': rest,\n"
        # getattr rather than ns.session so a suite run against a CLI without the
        # flag reports the missing *flag* rather than dying inside the stub.
        "           'session': getattr(ns, 'session', None),\n"
        "           'repo': getattr(ns, 'repo', None),\n"
        "           'launch_uid': getattr(ns, 'launch_uid', None),\n"
        "           'source_session': os.environ.get('LMER_SOURCE_SESSION_ID'),\n"
        "           'allow_unsigned': os.environ.get('LMER_ALLOW_UNSIGNED_SOURCE'),\n"
        "           'metadata': json.loads(os.environ.get('LMER_SESSION_METADATA', '{}'))}\n"
        f"open({str(dump)!r}, 'w', encoding='utf-8').write(json.dumps(payload))\n"
        # Staying alive is how a test keeps the session's registry entry around; a
        # clean exit reaps it, which would race any assertion about a live one.
        "time.sleep(float(os.environ.get('FAKE_LMER_SLEEP') or 0))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, dump


@pytest.fixture
def fake_lmer(resume_recording_lmer):
    return resume_recording_lmer[0]


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


def wait_for(predicate, timeout=10.0):
    """Poll until *predicate* holds — the child runs asynchronously."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def plant_run(
    config,
    *,
    slug=None,
    host=HOST,
    project=PROJECT,
    taskdef=TASKDEF,
    target=TARGET,
    stop_reason=None,
    question=None,
    status="in-progress",
    recorded_slug=...,
    session_id=None,
    schema=1,
    extra="",
):
    """Write one run dir into the mirror the way a pushed run appears there.

    Written as YAML text rather than through ``run_state.write_state`` on purpose:
    the mirror is a *read* surface for the platform, and planting bytes is the only
    way a test can be sure nothing in the resume path wrote to it. Values go
    through ``json.dumps`` (valid YAML) so a target full of metacharacters is
    planted as the string it is rather than as accidental YAML.

    ``session_id`` plants the schema-form shape instead: the record is the SESSION
    dir ``<project>/_runs/<slug>/<session-id>/`` and ``_runs/<slug>/`` is a
    grouping level nothing is ever written at (D22). The ``{host}/{project}/``
    project dir is the host bucket an unnamed repo keeps (D9), so these trees need
    no registry; resolving a *registered* flat short name is
    ``workrepo.resolve_run_dir``'s own contract and is covered where it lives.
    """
    slug = slug if slug is not None else run_state.derive_slug(taskdef, target)
    if session_id is None:
        path = config.mirror_path / host / project / "runs" / slug
    else:
        path = config.mirror_path / host / project / "_runs" / slug / session_id
    path.mkdir(parents=True, exist_ok=True)
    lines = [f"schema: {schema}", f"status: {json.dumps(status)}"]
    recorded = slug if recorded_slug is ... else recorded_slug
    for name, value in (
        ("slug", recorded), ("taskdef", taskdef), ("target", target),
        ("stop_reason", stop_reason), ("open_question", question),
    ):
        if value is not None:
            lines.append(f"{name}: {json.dumps(value)}")
    if extra:
        lines.append(extra)
    (path / "state.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "events.jsonl").write_text(
        json.dumps({"ts": "2026-07-27T10:00:00Z", "type": "run_seeded"}) + "\n",
        encoding="utf-8",
    )
    return path


def track_run(*, slug=None, taskdef=TASKDEF, target=TARGET, source="spawned",
              repo=REPO):
    slug = slug if slug is not None else run_state.derive_slug(taskdef, target)
    return runs.track(
        HOST, PROJECT, slug, source=source, taskdef=taskdef, target=target, repo=repo
    )


@pytest.fixture
def stopped_run(config):
    """A tracked run, present in the mirror, stopped at a phase boundary.

    ``stop_reason=yield`` is the row the fleet view labels "requires your review"
    — the state the operator adopted a run into and found no verb for.
    """
    plant_run(config, stop_reason="yield")
    return track_run()


def request_for(slug=None, **fields):
    return resume_mod.ResumeRequest(
        host=HOST,
        project=PROJECT,
        slug=slug if slug is not None else run_state.derive_slug(TASKDEF, TARGET),
        **fields,
    )


def child_payload(dump, timeout=10.0):
    """What the stub recorded, once it has run."""
    assert wait_for(lambda: dump.is_file() and dump.stat().st_size, timeout=timeout), (
        "the resuming session never started"
    )
    return json.loads(dump.read_text(encoding="utf-8"))


def plant_schema_two_source(config, *, signed_out=False):
    """One opaque schema-2 source record with launch provenance."""
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        "schema: 2\nname: example-plexus\nprojects: {}\n", encoding="utf-8"
    )
    source = plant_run(
        config, session_id="a" * 32, schema=2, stop_reason="yield",
        extra=(
            "session:\n"
            "  uid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "  operator: alice\n"
            "  hostname: build.example\n"
            "  started_at: 2026-09-17T12:00:00Z"
        ),
    )
    events = [
        {"ts": "2026-09-17T12:00:00Z", "type": "session_start"},
        {"ts": "2026-09-17T12:05:00Z", "type": "phase_set"},
    ]
    if signed_out:
        events.append({"ts": "2026-09-17T12:06:00Z", "type": "session_end"})
    (source / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return source


def snapshot_tree(root):
    """Every file under *root* as ``{relative path: bytes}``.

    The D3 guard's instrument: the platform must not write run state, and the
    mirror is the only run state it can reach. Contents rather than mtimes, since
    a rewrite with identical bytes would still be a write the platform must not
    make — and any *new* file shows up as a new key.
    """
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- continuing the run in front of the operator -----------------------------

def test_continuing_spawns_the_runs_own_taskdef_and_target(
    config, stopped_run, resume_recording_lmer
):
    """The one-tap case: no taskdef named, so the run's recorded one is used."""
    _script, dump = resume_recording_lmer
    result = resume_mod.resume_run(config, request_for())

    payload = child_payload(dump)
    assert payload["task"] == TASKDEF
    assert payload["target"] == [TARGET]
    assert payload["rest"] == []
    assert result.continued is True
    assert result.started_slug == stopped_run.slug
    assert (result.taskdef, result.target) == (TASKDEF, TARGET)


def test_continuing_passes_no_direction_when_none_was_given(
    config, stopped_run, resume_recording_lmer
):
    """A seed changes what the session does, so it must not appear uninvited."""
    _script, dump = resume_recording_lmer
    resume_mod.resume_run(config, request_for())

    payload = child_payload(dump)
    assert payload["prompt"] is None
    assert not any(arg.startswith(resume_mod.DIRECTION_FLAG) for arg in payload["argv"])
    assert payload["answer"] is None, "resume is the path without an answer"


def test_the_resuming_session_runs_with_a_control_plane(config, stopped_run):
    """Spec D8: whatever the platform spawns must be reachable and writable."""
    result = resume_mod.resume_run(config, request_for())
    assert "--fastapi" in result.session.command
    assert result.session.control_port


def test_the_run_is_joined_back_to_the_session_it_started(config, stopped_run,
                                                          monkeypatch):
    # Keep the child alive for the registry assertion: a clean exit reaps its
    # entry, so without this the last asserts race the watcher thread.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = resume_mod.resume_run(config, request_for())
    try:
        tracked = runs.get_tracked(HOST, PROJECT, stopped_run.slug)
        assert tracked.last_session_id == result.session.session_id, (
            "the detail view follows last_session_id, so it must point at the "
            "session the resume started"
        )
        assert tracked.source == "spawned", "resuming must not re-source the run"
        entry = registry.read_session(result.session.session_id)
        assert entry["run"] == {
            "host": HOST, "project": PROJECT, "slug": stopped_run.slug,
        }
    finally:
        os.kill(result.session.pid, 9)


def test_a_renamed_run_dir_still_targets_the_recorded_slug(config):
    """A renamed dir resolves by recorded slug, which is what the respawn derives.

    ``work name`` renames the directory; the state file keeps the original slug and
    that is what the container resolves on — so the directory name must not be what
    the landing-run check compares against.
    """
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)
    track_run(slug=f"{recorded}--nice-name")

    result = resume_mod.resume_run(config, request_for(slug=f"{recorded}--nice-name"))

    assert result.continued is True
    assert result.started_slug == recorded
    assert result.session.slug == recorded
    assert runs.get_tracked(HOST, PROJECT, f"{recorded}--nice-name").last_session_id == (
        result.session.session_id
    ), "the key the operator resumed must follow the session that was started"


def test_a_state_file_with_no_slug_is_matched_on_its_directory_name(config):
    """Nothing but the directory name can resolve such a run, so that is the check."""
    slug = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, recorded_slug=None)
    track_run()

    result = resume_mod.resume_run(config, request_for())
    assert (result.continued, result.started_slug) == (True, slug)


def test_an_identity_with_stray_whitespace_still_resolves(config, stopped_run):
    """The index strips and a filesystem path does not, so the two would disagree.

    A copy-pasted identity is the ordinary way to get here, and the failure it
    would otherwise produce ("not in the host mirror") sends debugging at the
    mirror instead of at the whitespace.
    """
    result = resume_mod.resume_run(
        config,
        resume_mod.ResumeRequest(
            host=f" {HOST} ", project=f"{PROJECT}\t", slug=f" {stopped_run.slug}",
        ),
    )
    assert result.started_slug == stopped_run.slug


@pytest.mark.parametrize("stop_reason", [None, "paused", "yield", "critical_error"])
def test_every_stop_that_is_not_a_question_is_resumable(config, stop_reason):
    """Parked, yielded and failed runs are exactly what this verb is for."""
    plant_run(config, stop_reason=stop_reason)
    track_run()
    assert resume_mod.resume_run(config, request_for()).continued is True


# --- the override starts a sibling, and says so ------------------------------

def test_an_override_starts_the_sibling_run_and_says_which(
    config, stopped_run, resume_recording_lmer
):
    """``develop`` → ``review`` against one target: a different run, not this one."""
    _script, dump = resume_recording_lmer
    result = resume_mod.resume_run(config, request_for(taskdef="review"))

    sibling = run_state.derive_slug("review", TARGET)
    assert sibling != stopped_run.slug
    assert result.continued is False
    assert result.started_slug == sibling
    assert result.session.slug == sibling, (
        "the slug the reply names has to be the one the spawn derived, or the two "
        "halves of this feature disagree about which run was started"
    )
    assert child_payload(dump)["task"] == "review"

    payload = result.to_dict()
    assert payload["run"]["slug"] == stopped_run.slug
    assert payload["started"]["slug"] == sibling
    assert payload["continued"] is False
    assert "untouched" in payload["note"], (
        "the reply has to say the operator's run was not the one continued"
    )


def test_the_sibling_session_is_not_joined_to_the_run_it_came_from(
    config, stopped_run, monkeypatch
):
    """Pointing the source run at a session belonging to another run is a lie.

    The spawn tracks the run *it* derived, which is the sibling; the source run
    keeps whatever session it last had — here, none.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = resume_mod.resume_run(config, request_for(taskdef="review"))
    try:
        assert runs.get_tracked(HOST, PROJECT, stopped_run.slug).last_session_id is None
        sibling = runs.get_tracked(HOST, PROJECT, result.started_slug)
        assert sibling is not None, "the sibling run must be tracked by the spawn"
        assert sibling.last_session_id == result.session.session_id
        assert sibling.taskdef == "review"
    finally:
        os.kill(result.session.pid, 9)


def test_an_override_naming_the_recorded_taskdef_is_still_a_continue(
    config, stopped_run
):
    """It derives the same slug, so calling it a sibling would be a lie."""
    result = resume_mod.resume_run(config, request_for(taskdef=TASKDEF))
    assert (result.continued, result.started_slug) == (True, stopped_run.slug)


@pytest.mark.parametrize("status", ["complete", "archived"])
def test_a_finished_run_can_start_a_sibling_with_no_direction(config, status):
    """The workflow this override exists for: review a develop run that is done.

    The finished-run contract is about the run the session lands in, and that run
    is the review — which has never been seeded. Checking the *source* run's status
    would refuse the override's main use case.
    """
    plant_run(config, status=status)
    track_run()

    result = resume_mod.resume_run(config, request_for(taskdef="review"))
    assert result.continued is False
    assert result.started_slug == run_state.derive_slug("review", TARGET)


def test_a_question_blocked_run_can_still_start_a_sibling(config):
    """The question belongs to the run being left alone, so it blocks nothing here."""
    plant_run(config, stop_reason="question", question=QUESTION)
    track_run()

    result = resume_mod.resume_run(config, request_for(taskdef="review"))
    assert result.continued is False
    state = (
        config.mirror_path / HOST / PROJECT / "runs" / request_for().slug / "state.yaml"
    ).read_text(encoding="utf-8")
    assert "question" in state, "the source run keeps the stop it is in"


def test_an_existing_sibling_governs_the_checks_instead_of_the_source(config):
    """Once the sibling exists, this spawn is a resume *of it* to the container."""
    plant_run(config)
    track_run()
    plant_run(config, taskdef="review", status="complete")

    with pytest.raises(resume_mod.DirectionRequired, match="review-issue-141") as caught:
        resume_mod.resume_run(config, request_for(taskdef="review"))
    assert caught.value.code == "direction_required"
    assert registry.list_sessions(live_only=False) == []


def test_a_question_blocked_sibling_is_sent_to_the_answer_flow(config, stopped_run):
    """The rule is "the landing run governs", so the question check follows it."""
    plant_run(config, taskdef="review", stop_reason="question", question=QUESTION)

    with pytest.raises(resume_mod.QuestionOpen, match="review-issue-141"):
        resume_mod.resume_run(config, request_for(taskdef="review"))
    assert registry.list_sessions(live_only=False) == []


def test_an_existing_sibling_with_unreadable_state_is_refused(config, stopped_run):
    """Its state is what decides; unable to read it is unable to decide."""
    sibling = plant_run(config, taskdef="review")
    (sibling / "state.yaml").write_text("not: [a, mapping\n", encoding="utf-8")

    with pytest.raises(resume_mod.NotResumable, match="could not be read"):
        resume_mod.resume_run(config, request_for(taskdef="review"))
    assert registry.list_sessions(live_only=False) == []


def test_an_existing_sibling_with_a_live_session_is_refused(config, stopped_run):
    """Otherwise the override is a way around the duplicate-container check."""
    sibling = run_state.derive_slug("review", TARGET)
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": sibling},
    )
    with pytest.raises(resume_mod.RunIsLive, match=sibling):
        resume_mod.resume_run(config, request_for(taskdef="review"))
    assert len(registry.list_sessions(live_only=False)) == 1


# --- argv fidelity -----------------------------------------------------------

@pytest.mark.parametrize("target", [
    "feature/spaces in the branch name",
    "fix/$(whoami) && echo pwned | tee /tmp/x",
    "hotfix/quote'inside\"and;semis",
    "branch\twith\ttabs",
    "https://gitlab.example.com/agents/global/-/merge_requests/154?tab=diffs#note_1",
    "naïve-branch-… ✓",
])
def test_a_target_survives_being_argv_not_a_shell_string(
    config, resume_recording_lmer, target
):
    """It is argv all the way down, so no quoting rule can eat any of this."""
    _script, dump = resume_recording_lmer
    slug = run_state.derive_slug(TASKDEF, target)
    plant_run(config, target=target)
    track_run(target=target)

    result = resume_mod.resume_run(config, request_for(slug=slug))

    payload = child_payload(dump)
    assert payload["target"] == [target]
    assert payload["task"] == TASKDEF
    assert payload["rest"] == [], (
        "no part of the target may be mistaken for lmer's own arguments"
    )
    assert result.continued is True


@pytest.mark.parametrize("direction", [
    "review the MR and only comment on the migration",
    "-1 means start over",
    "--force is what I meant",
    'he said "yes" — use $HOME/;rm -rf /`id`',
    "line one\nline two\n\n  indented three",
    "--answer=not an answer",
])
def test_a_direction_survives_as_one_flag_token(
    config, resume_recording_lmer, direction
):
    """``--prompt -1`` makes argparse exit 2: the value looks like an option.

    So the ``=`` spelling is not a style choice — and the last case is the one that
    matters most: a direction that spells another flag is still a direction, never
    a second flag, because it is inside the token.
    """
    _script, dump = resume_recording_lmer
    plant_run(config, status="complete")
    track_run()

    resume_mod.resume_run(config, request_for(direction=direction))

    payload = child_payload(dump)
    assert payload["prompt"] == direction
    assert payload["answer"] is None
    assert f"{resume_mod.DIRECTION_FLAG}={direction}" in payload["argv"]
    assert resume_mod.DIRECTION_FLAG not in payload["argv"], (
        "a bare --prompt token means the text was passed as a separate argument"
    )
    assert payload["rest"] == []


def test_surrounding_whitespace_is_normalised_away(
    config, stopped_run, resume_recording_lmer
):
    """The brief renders the seed on one line, so this is what it would show."""
    _script, dump = resume_recording_lmer
    resume_mod.resume_run(config, request_for(direction="  \n do the thing \n "))
    assert child_payload(dump)["prompt"] == "do the thing"


def test_the_project_fields_are_normalised_like_every_other_field():
    """The merge put two branches' field sets on one request; both are cleaned."""
    cleaned = request_for(
        workspace_id="  beta  ", home_revision=2, confirm_project=True, slot="  web  ",
    ).validate()
    assert cleaned.workspace_id == "beta"
    assert cleaned.home_revision == 2
    assert cleaned.confirm_project is True
    assert cleaned.slot == "web"


@pytest.mark.parametrize("field, value, message", [
    ("workspace_id", 7, "workspace_id must be a string"),
    ("slot", 7, "slot must be a string"),
    ("home_revision", "2", "home_revision must be a non-negative integer"),
    ("home_revision", True, "home_revision must be a non-negative integer"),
    ("home_revision", -1, "home_revision must be a non-negative integer"),
    ("confirm_project", "true", "confirm_project must be a boolean"),
])
def test_the_project_fields_are_refused_rather_than_coerced(field, value, message):
    """A truthy ``confirm_project`` string is the failure mode worth refusing."""
    with pytest.raises(resume_mod.ResumeError, match=message):
        request_for(**{field: value}).validate()


# --- the platform writes no run state (spec D3) ------------------------------

def test_resuming_writes_nothing_into_the_mirror(config, stopped_run):
    """The mirror is a read-only clone the daemon force-resets; a write there is
    both forbidden and futile. The *session* records what it does, in the container.
    """
    before = snapshot_tree(config.mirror_path)
    assert before, "the planted run must be in the snapshot for this to prove anything"

    resume_mod.resume_run(config, request_for())

    assert snapshot_tree(config.mirror_path) == before, (
        "the platform must not touch state.yaml, events.jsonl or anything else "
        "under the mirror (spec D3)"
    )


def test_reopening_a_finished_run_is_left_to_the_session(config):
    """The status is the container's to change, on the contract's terms."""
    plant_run(config, status="complete")
    track_run()
    state_file = (
        config.mirror_path / HOST / PROJECT / "runs" / request_for().slug / "state.yaml"
    )

    resume_mod.resume_run(config, request_for(direction="finish the changelog"))

    assert '"complete"' in state_file.read_text(encoding="utf-8")


# --- the direction is content, not something to copy around ------------------

def test_the_direction_never_reaches_the_platform_event_log(config, stopped_run):
    secret_ish = "use the staging db password hunter2 to reproduce it"
    result = resume_mod.resume_run(config, request_for(direction=secret_ish))

    events = store.read_events()
    assert secret_ish not in json.dumps(events)
    resumed = [e for e in events if e["type"] == "run_resumed"][-1]
    assert resumed["data"]["direction_chars"] == len(secret_ish)
    assert resumed["data"]["run"]["slug"] == stopped_run.slug
    assert resumed["data"]["started"]["slug"] == stopped_run.slug
    assert resumed["data"]["continued"] is True
    assert resumed["data"]["session"] == result.session.session_id


def test_the_direction_is_not_echoed_back_to_the_caller(config, stopped_run):
    """``SpawnResult.to_dict`` publishes the argv, and the argv carries the seed."""
    text = "direction body that must not come back"
    result = resume_mod.resume_run(config, request_for(direction=text))

    payload = json.dumps(result.to_dict())
    assert text not in payload
    assert "command" not in payload, (
        "publishing the spawn's command would publish --prompt=<text> with it"
    )
    assert result.to_dict()["session"]["session_id"] == result.session.session_id


def test_the_direction_is_not_logged(config, stopped_run, caplog):
    caplog.set_level("INFO")
    text = "do not log me anywhere"
    resume_mod.resume_run(config, request_for(direction=text))
    assert any("platform_run_resumed" in r.message for r in caplog.records)
    assert all(text not in r.getMessage() for r in caplog.records)


# --- refusals: scope and readability -----------------------------------------

def test_an_untracked_run_is_refused(config):
    """Scope is the local index (D25): a colleague's run is not ours to restart."""
    plant_run(config)
    with pytest.raises(resume_mod.RunNotTracked, match="not tracked") as caught:
        resume_mod.resume_run(config, request_for())
    assert (caught.value.status, caught.value.code) == (404, "run_not_tracked")
    assert registry.list_sessions(live_only=False) == []


def test_a_run_missing_from_the_mirror_is_refused(config):
    track_run()
    with pytest.raises(resume_mod.NotResumable, match="not in the host mirror"):
        resume_mod.resume_run(config, request_for())
    assert registry.list_sessions(live_only=False) == []


def test_a_refusal_names_the_run_key_not_a_directory_it_guessed(config):
    """Refusals used to compose ``runs/<slug>`` out of the request, and a named run
    has no such directory — it is at ``runs/<slug>--<name>`` (T90). The key is what
    the run is tracked under and what a corrected request repeats; a resolved run
    dir names itself, and only then is a path in a message a real one."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)

    with pytest.raises(resume_mod.RunNotTracked) as caught:
        resume_mod.resume_run(config, request_for(slug=recorded))

    message = str(caught.value)
    assert message.startswith(f"{HOST}/{PROJECT}/{recorded} is not tracked")
    assert f"runs/{recorded}" not in message, "no directory is invented here"


def test_the_live_session_refusal_names_the_run_key_too(config):
    """The other message built from the request, and the one whose slug is not even
    the request's: it is the identity of the run the session would land in."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded,
              stop_reason="yield")
    track_run(slug=recorded)
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": recorded},
    )

    with pytest.raises(resume_mod.RunIsLive) as caught:
        resume_mod.resume_run(config, request_for(slug=recorded))

    message = str(caught.value)
    assert message.startswith(f"{HOST}/{PROJECT}/{recorded} already has a live session")
    assert f"runs/{recorded}" not in message


def test_a_run_with_unreadable_state_is_refused(config):
    plant_run(config)
    track_run()
    path = config.mirror_path / HOST / PROJECT / "runs" / request_for().slug
    (path / "state.yaml").write_text("not: [a, mapping\n", encoding="utf-8")

    with pytest.raises(resume_mod.NotResumable, match="could not be read") as caught:
        resume_mod.resume_run(config, request_for())
    assert "nothing to continue from" in str(caught.value)
    assert "answerable" not in str(caught.value), (
        "the refusal is borrowed from the answer path; its wording must not be"
    )
    assert registry.list_sessions(live_only=False) == []


def test_a_run_whose_state_vanishes_mid_resume_is_refused(config, stopped_run,
                                                          monkeypatch):
    """The mirror is force-reset under this path, not frozen for it.

    Every fleet poll calls ``pull()``, which ``git reset --hard``s the mirror — so
    the state file really can disappear between resolving the run dir and reading
    it. The difference between a refusal and an ``AttributeError`` 500 is that
    branch.
    """
    resolve = resume_mod.resolve_run_dir

    def resolve_then_reset(*args, **kwargs):
        ref = resolve(*args, **kwargs)
        (ref.path / "state.yaml").unlink()
        return ref

    monkeypatch.setattr(resume_mod, "resolve_run_dir", resolve_then_reset)

    with pytest.raises(resume_mod.NotResumable, match="could not be read"):
        resume_mod.resume_run(config, request_for())
    assert registry.list_sessions(live_only=False) == []


# --- refusals: a second container for one run --------------------------------

def test_a_run_with_a_live_session_is_refused(config, stopped_run):
    """Liveness outranks committed state (D24), and two containers for one run
    would fight over its owner claim."""
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": stopped_run.slug},
    )
    with pytest.raises(resume_mod.RunIsLive, match="already has a live session") as caught:
        resume_mod.resume_run(config, request_for())
    assert (caught.value.status, caught.value.code) == (409, "live_session")
    assert "s-live" in str(caught.value)
    assert len(registry.list_sessions(live_only=False)) == 1


def test_a_live_session_under_a_renamed_runs_recorded_slug_is_seen(config):
    """The identity a session registers under is the recorded slug, not the dir name."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)
    track_run(slug=f"{recorded}--nice-name")
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": recorded},
    )

    with pytest.raises(resume_mod.RunIsLive, match="already has a live session"):
        resume_mod.resume_run(config, request_for(slug=f"{recorded}--nice-name"))
    assert len(registry.list_sessions(live_only=False)) == 1


def test_a_live_session_under_a_renamed_runs_directory_name_is_seen(config):
    """And the other spelling: a session filed under the name the operator sees."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)
    track_run(slug=f"{recorded}--nice-name")
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": f"{recorded}--nice-name"},
    )

    with pytest.raises(resume_mod.RunIsLive, match="already has a live session"):
        resume_mod.resume_run(config, request_for(slug=f"{recorded}--nice-name"))
    assert len(registry.list_sessions(live_only=False)) == 1


def test_a_dead_session_does_not_block_a_resume(config, stopped_run):
    """A stale entry is a crash signal, not a duplicate-spawn risk — and a crashed
    run is one of the rows this verb exists to restart."""
    registry.register(
        "s-dead", pid=2**22,
        run={"host": HOST, "project": PROJECT, "slug": stopped_run.slug},
    )
    assert resume_mod.resume_run(config, request_for()).session.session_id


def test_resuming_twice_is_refused_by_the_live_session_check(
    config, stopped_run, monkeypatch
):
    """Which is also what makes a double-tapped continue button harmless.

    The first session is kept alive deliberately: it is the live entry the second
    attempt has to trip over, and a stub that exits immediately would let the
    second resume through for reasons that have nothing to do with the guard.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    first = resume_mod.resume_run(config, request_for())
    try:
        assert wait_for(
            lambda: registry.is_live(registry.read_session(first.session.session_id))
        )
        with pytest.raises(resume_mod.RunIsLive, match="already has a live session"):
            resume_mod.resume_run(config, request_for())
        assert len(registry.list_sessions(live_only=True)) == 1
    finally:
        os.kill(first.session.pid, 9)


def test_the_spawn_still_refuses_a_duplicate_with_this_check_defeated(
    config, stopped_run, monkeypatch
):
    """Where the rule actually lives, and why this module's copy is not the fix.

    The refusals above are this route's wording and its ``live_session`` code, which
    is what a client reads; the *invariant* is in ``spawn_session``, on the identity
    the spawn is about to register. So a caller that reaches the spawn without a
    check of its own — which is what ``POST /api/sessions`` was — is refused there,
    and defeating the guard above is how a test can stand in that caller's place.
    """
    monkeypatch.setattr(resume_mod, "_refuse_if_live", lambda *_a, **_kw: None)
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": stopped_run.slug},
    )

    with pytest.raises(spawn.RunAlreadyLive, match="already has a live session"):
        resume_mod.resume_run(config, request_for())
    assert len(registry.list_sessions(live_only=False)) == 1, (
        "the resume started a second session for a run that already had one"
    )


# --- refusals: a question belongs to the answer verb -------------------------

def test_a_question_blocked_run_is_sent_to_the_answer_flow(config):
    """A plain respawn reads the question back and leaves the stop in place."""
    plant_run(config, stop_reason="question", question=QUESTION)
    track_run()

    with pytest.raises(resume_mod.QuestionOpen, match="stopped on a question") as caught:
        resume_mod.resume_run(config, request_for())
    assert (caught.value.status, caught.value.code) == (409, "question_open")
    assert "POST /api/runs/answer" in str(caught.value), (
        "the refusal has to name the verb that can actually unblock the run"
    )
    assert registry.list_sessions(live_only=False) == []


def test_a_question_stop_with_no_recorded_text_is_refused_the_same_way(config):
    """The commonest shape of a question stop; the missing text changes nothing."""
    plant_run(config, stop_reason="question")
    track_run()

    with pytest.raises(resume_mod.QuestionOpen):
        resume_mod.resume_run(config, request_for())


# --- refusals: a finished run reopens on a direction -------------------------

@pytest.mark.parametrize("status", ["complete", "archived"])
def test_a_finished_run_without_a_direction_is_refused(config, status):
    """Silently reopening is wrong (#96) and so is having no way to say yes."""
    plant_run(config, status=status)
    track_run()

    with pytest.raises(resume_mod.DirectionRequired, match=f"is {status}") as caught:
        resume_mod.resume_run(config, request_for())
    assert (caught.value.status, caught.value.code) == (400, "direction_required")
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t "])
def test_a_blank_direction_is_no_direction(config, blank):
    """The field is a text box the ordinary continue submits empty."""
    plant_run(config, status="complete")
    track_run()

    with pytest.raises(resume_mod.DirectionRequired):
        resume_mod.resume_run(config, request_for(direction=blank))


@pytest.mark.parametrize("status", ["complete", "archived"])
def test_a_finished_run_with_a_direction_is_resumed(
    config, status, resume_recording_lmer
):
    """The contract's own mechanism: the seed is what the session reopens on."""
    _script, dump = resume_recording_lmer
    plant_run(config, status=status)
    track_run()

    result = resume_mod.resume_run(
        config, request_for(direction="write the changelog entry and close it out")
    )

    assert result.continued is True
    assert child_payload(dump)["prompt"] == (
        "write the changelog entry and close it out"
    )


def test_the_direction_flag_is_the_one_the_session_reads_as_a_seed():
    """``work session-start`` threads ``LMER_START_PROMPT`` into the resume brief.

    Source-level because the two ends live in different packages: the flag this
    module spells has to be the flag ``lmer`` maps onto that variable, or a
    finished run is reopened on a direction the session never sees.
    """
    cli = (Path(__file__).resolve().parent.parent / "src" / "lmer_cli" / "cli.py")
    assert f'"{resume_mod.DIRECTION_FLAG}", dest="prompt"' in cli.read_text(
        encoding="utf-8"
    )
    work_cli = (
        Path(__file__).resolve().parent.parent / "src" / "work_repo" / "cli.py"
    ).read_text(encoding="utf-8")
    assert 'seed=os.environ.get("LMER_START_PROMPT")' in work_cli


def test_an_oversized_direction_is_refused_before_the_spawn_fails(config, stopped_run):
    """Otherwise ``execve`` answers with an opaque "Argument list too long"."""
    with pytest.raises(resume_mod.ResumeError, match="over the"):
        resume_mod.resume_run(
            config,
            request_for(direction="x" * (resume_mod.MAX_DIRECTION_CHARS + 1)),
        )
    assert registry.list_sessions(live_only=False) == []


def test_a_direction_at_the_limit_is_accepted(config, stopped_run):
    result = resume_mod.resume_run(
        config, request_for(direction="x" * resume_mod.MAX_DIRECTION_CHARS)
    )
    assert result.session.session_id


def test_the_limit_stays_under_the_kernel_argument_ceiling():
    """MAX_ARG_STRLEN is 128 KiB; four bytes per character is the UTF-8 worst case."""
    assert resume_mod.MAX_DIRECTION_CHARS * 4 < 131072


# --- refusals: the repo URL is asked for, never invented ---------------------

def test_an_adopted_run_with_no_repo_url_is_refused_recognisably(config):
    """The UI turns this into "give me the repo URL", so it needs a stable code."""
    plant_run(config)
    slug = run_state.derive_slug(TASKDEF, TARGET)
    runs.track(HOST, PROJECT, slug, source="adopted", taskdef=TASKDEF, target=TARGET)

    with pytest.raises(resume_mod.RepoUrlRequired, match="no repo URL") as caught:
        resume_mod.resume_run(config, request_for())
    assert (caught.value.status, caught.value.code) == (400, "repo_url_required")
    assert caught.value.to_dict() == {
        "code": "repo_url_required", "message": str(caught.value),
    }
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("url", [
    "https://gitlab.example.com/agents/global",
    "https://gitlab.example.com/agents/global.git",
    "git@gitlab.example.com:agents/global.git",
])
def test_a_supplied_repo_url_satisfies_the_refusal(config, url):
    """Any spelling that parses back to the run's own host and project."""
    plant_run(config)
    slug = run_state.derive_slug(TASKDEF, TARGET)
    runs.track(HOST, PROJECT, slug, source="adopted", taskdef=TASKDEF, target=TARGET)

    result = resume_mod.resume_run(config, request_for(repo_url=url))

    assert result.session.host == HOST
    assert result.session.project == PROJECT
    assert result.session.slug == slug


def test_a_supplied_repo_url_is_remembered_for_the_next_resume(config):
    """Otherwise the operator is asked the same question on every continue."""
    plant_run(config)
    slug = run_state.derive_slug(TASKDEF, TARGET)
    runs.track(HOST, PROJECT, slug, source="adopted", taskdef=TASKDEF, target=TARGET)

    resume_mod.resume_run(config, request_for(repo_url=REPO))
    assert runs.get_tracked(HOST, PROJECT, slug).repo == REPO

    # The first session has to be gone before the second resume, or the live-run
    # check refuses it for a reason this test is not about.
    assert wait_for(lambda: not registry.list_sessions(live_only=True))
    assert resume_mod.resume_run(config, request_for()).continued is True


def test_a_supplied_repo_url_corrects_a_recorded_one(config, stopped_run):
    """The only path by which a wrong recorded URL can be fixed."""
    corrected = "https://gitlab.example.com/agents/global"
    track_run(repo="https://gitlab.example.com/agents/global.git")

    resume_mod.resume_run(config, request_for(repo_url=corrected))
    assert runs.get_tracked(HOST, PROJECT, stopped_run.slug).repo == corrected


@pytest.mark.parametrize("url", [
    "https://github.com/agents/global",
    "https://gitlab.example.com/someone/else",
    "git@gitlab.example.com:agents/other.git",
    "not a url at all",
    "https://",
])
def test_a_repo_url_naming_another_project_is_refused(config, stopped_run, url):
    """``derive_run_identity`` files the session by this URL, so a wrong one
    orphans it under a run nobody is looking at."""
    with pytest.raises(resume_mod.RepoUrlRequired, match="but the run is") as caught:
        resume_mod.resume_run(config, request_for(repo_url=url))
    assert "supplied" in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


def test_a_recorded_repo_url_naming_another_project_is_refused(config):
    """Same harm, and the message says where the bad value came from."""
    plant_run(config)
    track_run(repo="https://gitlab.example.com/someone/else")

    with pytest.raises(resume_mod.RepoUrlRequired, match="recorded for this run"):
        resume_mod.resume_run(config, request_for())
    assert registry.list_sessions(live_only=False) == []


def test_the_ambient_repo_url_is_not_a_substitute(config, monkeypatch):
    """``spawn`` falls back to ``LMER_REPO_URL``; that names whatever repo the
    daemon was launched for, which is a guess with a different accent."""
    monkeypatch.setenv("LMER_REPO_URL", REPO)
    plant_run(config)
    slug = run_state.derive_slug(TASKDEF, TARGET)
    runs.track(HOST, PROJECT, slug, source="adopted", taskdef=TASKDEF, target=TARGET)

    with pytest.raises(resume_mod.RepoUrlRequired):
        resume_mod.resume_run(config, request_for())


# --- a project reached through more than one remote (issue #353, D8) ---------
#
# A registered project with ``remotes:`` is ONE project at several URLs. The
# launch records the URL it was given, while ``spawn.derive_run_identity`` folds
# that URL to the project's PRIMARY remote so the run is tracked under the same
# key the mirror walk lists its directory by (tests/test_platform_spawn.py). The
# recorded URL and the tracked identity therefore differ *by design* for such a
# run, and the check below compares what the next spawn will DERIVE rather than
# what the URL parses to on its own — otherwise resume would refuse exactly the
# runs the fold exists to key correctly, and refuse them with a message accusing
# the operator of naming another project.

WIDGETS_PRIMARY = "gitlab.example.com/acme/widgets"
WIDGETS_SECONDARY = "github.com/acme/widgets"
WIDGETS_URL = "https://github.com/acme/widgets.git"
WIDGETS_TARGET = "https://gitlab.example.com/acme/widgets/-/work_items/42"


def plant_widgets_manifest(config):
    """A registry that knows ``widgets`` at both of its remotes (D8). Planted
    before the identity is derived, because it is what the fold reads."""
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        "schema: 1\nname: example-plexus\nprojects:\n  widgets:\n"
        f"    repo: {WIDGETS_PRIMARY}\n    remotes: [{WIDGETS_SECONDARY}]\n",
        encoding="utf-8",
    )


def plant_widgets_run(config, slug):
    """The record a launch through the SECONDARY remote left: under the project's
    flat short name, which is where the registry's union placed it (D7/D22)."""
    path = config.mirror_path / "widgets" / "_runs" / slug / SESSION_ID
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.yaml").write_text(
        "\n".join([
            "schema: 1",
            'status: "in-progress"',
            f"slug: {json.dumps(slug)}",
            f"taskdef: {json.dumps(TASKDEF)}",
            f"target: {json.dumps(WIDGETS_TARGET)}",
            'stop_reason: "yield"',
        ]) + "\n",
        encoding="utf-8",
    )
    return path


def test_a_run_launched_through_a_secondary_remote_still_resumes(config):
    """Its recorded repo names the mirror, its identity names the primary, and
    both describe the same run — so this must start a session, not refuse."""
    plant_widgets_manifest(config)
    host, project, slug = spawn.derive_run_identity(
        spawn.SpawnRequest(
            taskdef=TASKDEF, target=WIDGETS_TARGET, repo_url=WIDGETS_URL,
        ),
        WIDGETS_URL, config,
    )
    plant_widgets_run(config, slug)
    runs.track(host, project, slug, taskdef=TASKDEF, target=WIDGETS_TARGET,
               repo=WIDGETS_URL)

    result = resume_mod.resume_run(config, resume_mod.ResumeRequest(
        host=host, project=project, slug=slug,
    ))

    assert result.session.session_id, (
        "a run reachable at two remotes was refused its own repo URL"
    )


# --- refusals: what the spawn would derive -----------------------------------

def test_a_resume_that_would_derive_another_run_is_refused(config):
    """The index and the state file disagree with the run's recorded slug — the
    hand-edited or mis-adopted case. Starting a run nobody named is the surprise."""
    plant_run(config, slug="develop-issue-999", recorded_slug="develop-issue-999")
    runs.track(HOST, PROJECT, "develop-issue-999", source="adopted",
               taskdef=TASKDEF, target=TARGET, repo=REPO)

    with pytest.raises(resume_mod.NotResumable, match="land on a different run") as caught:
        resume_mod.resume_run(config, request_for(slug="develop-issue-999"))
    assert "Pass a taskdef explicitly" in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


def test_resume_follows_a_run_that_reslugged_from_the_tracked_key(config):
    base = run_state.derive_slug(TASKDEF, TARGET)
    current = f"{base}-v1.2.3"
    plant_run(
        config,
        slug=current,
        recorded_slug=current,
        stop_reason="yield",
        extra=f"reslugged_from:\n  - {base}",
    )
    track_run(slug=base)

    result = resume_mod.resume_run(config, request_for(slug=base))

    assert result.continued is True
    assert result.started_slug == base


def test_the_same_mismatch_is_allowed_once_a_taskdef_is_named(config):
    """Because then the operator said which run they meant to start."""
    plant_run(config, slug="develop-issue-999", recorded_slug="develop-issue-999")
    runs.track(HOST, PROJECT, "develop-issue-999", source="adopted",
               taskdef=TASKDEF, target=TARGET, repo=REPO)

    result = resume_mod.resume_run(
        config, request_for(slug="develop-issue-999", taskdef=TASKDEF)
    )
    assert (result.continued, result.started_slug) == (False, "develop-issue-141")


@pytest.mark.parametrize("missing", [{"taskdef": None}, {"taskdef": None, "target": None}])
def test_a_run_with_no_recorded_taskdef_needs_one_named(config, missing):
    """An adopted run records nothing, and the index may know nothing either."""
    plant_run(config, slug="develop-orphan", recorded_slug="develop-orphan", **missing)
    runs.track(HOST, PROJECT, "develop-orphan", source="adopted", repo=REPO)

    with pytest.raises(resume_mod.NotResumable, match="cannot work out what to start"):
        resume_mod.resume_run(config, request_for(slug="develop-orphan"))
    assert registry.list_sessions(live_only=False) == []


def test_naming_a_taskdef_resumes_a_run_whose_state_records_none(config):
    """The override is the whole answer for a run nobody recorded a taskdef for."""
    slug = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=slug, taskdef=None)
    runs.track(HOST, PROJECT, slug, source="adopted", target=TARGET, repo=REPO)

    result = resume_mod.resume_run(config, request_for(taskdef=TASKDEF))
    assert (result.continued, result.started_slug) == (True, slug)


def test_the_index_fills_in_a_taskdef_the_state_file_lacks(config):
    """Old state files predate these fields; the index is what remembers them."""
    slug = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=slug, taskdef=None, target=None)
    track_run()

    result = resume_mod.resume_run(config, request_for())
    assert result.session.command[1:3] == [TASKDEF, TARGET]


def test_a_run_with_no_target_anywhere_is_refused(config):
    """A target-less run's slug is its taskdef, which every other one shares."""
    plant_run(config, slug="develop", recorded_slug="develop", target=None)
    runs.track(HOST, PROJECT, "develop", source="adopted", taskdef=TASKDEF, repo=REPO)

    with pytest.raises(resume_mod.NotResumable, match="records no target"):
        resume_mod.resume_run(config, request_for(slug="develop"))
    assert registry.list_sessions(live_only=False) == []


# --- refusals: values that would not survive being a path or an argv token ---

@pytest.mark.parametrize("identity", [
    {"slug": "../../../etc"},
    {"slug": ".."},
    {"project": "agents/../../../etc"},
    {"host": "../gitlab.example.com"},
    {"host": "a/b"},
    {"project": "agents//global"},
    {"slug": "..\\..\\windows"},
])
def test_an_identity_that_would_escape_the_mirror_is_refused(config, identity):
    """These become path segments under the mirror, and this verb starts something."""
    payload = {"host": HOST, "project": PROJECT, "slug": "develop-1"}
    payload.update(identity)
    request = resume_mod.ResumeRequest(**payload)
    runs.track(request.host, request.project, request.slug, source="adopted",
               taskdef=TASKDEF, target=TARGET, repo=REPO)

    with pytest.raises(resume_mod.ResumeError, match="path"):
        resume_mod.resume_run(config, request)
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("identity", [
    {"host": ""}, {"project": "  "}, {"slug": ""}, {"host": 5}, {"slug": None},
])
def test_an_incomplete_identity_is_refused(config, identity):
    payload = {"host": HOST, "project": PROJECT, "slug": "develop-1"}
    payload.update(identity)
    with pytest.raises(resume_mod.ResumeError) as caught:
        resume_mod.resume_run(config, resume_mod.ResumeRequest(**payload))
    assert caught.value.status == 400


@pytest.mark.parametrize("taskdef", [
    "../../etc/passwd", "review/../../escape", "..", ".", "a\\b",
])
def test_a_taskdef_that_would_escape_the_run_tree_is_refused(config, stopped_run,
                                                             taskdef):
    """``derive_slug`` interpolates the taskdef into the directory name unchecked —
    only the *target* goes through ``sanitize_task_target``."""
    with pytest.raises(resume_mod.ResumeError, match="path") as caught:
        resume_mod.resume_run(config, request_for(taskdef=taskdef))
    assert caught.value.status == 400
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("taskdef", [
    "--fastapi-token=known", "--no-supervisor", "-x", "--answer=smuggled",
])
def test_a_taskdef_that_argparse_would_read_as_a_flag_is_refused(
    config, stopped_run, taskdef
):
    """The taskdef is argv[1] to ``lmer``, ahead of every flag the platform sets.

    ``spawn._RESERVED_ARGS`` only scans ``extra_args``, so a flag spelled here
    would be parsed as that flag with the target sliding into the taskdef's place
    — the reserved-args guard, walked around by one position.
    """
    with pytest.raises(resume_mod.ResumeError, match="begin with a dash"):
        resume_mod.resume_run(config, request_for(taskdef=taskdef))
    assert registry.list_sessions(live_only=False) == []


def test_a_recorded_taskdef_that_looks_like_a_flag_is_refused(config):
    """The mirror is a repo every dev writes to; the platform reads it, it does not
    trust it. 409 rather than 400: the request was fine, the run's record is not."""
    plant_run(config, slug="weird", recorded_slug="weird", taskdef="--no-supervisor")
    runs.track(HOST, PROJECT, "weird", source="adopted", repo=REPO)

    with pytest.raises(resume_mod.NotResumable, match="begin with a dash") as caught:
        resume_mod.resume_run(config, request_for(slug="weird"))
    assert caught.value.status == 409
    assert registry.list_sessions(live_only=False) == []


def test_a_recorded_target_that_looks_like_a_flag_is_refused(config):
    """Same, one argv position along — and there is no override for the target."""
    plant_run(config, slug="weird", recorded_slug="weird", target="--fastapi-host=0.0.0.0")
    runs.track(HOST, PROJECT, "weird", source="adopted", repo=REPO)

    with pytest.raises(resume_mod.NotResumable, match="begin with a dash"):
        resume_mod.resume_run(config, request_for(slug="weird"))
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("field,value", [
    ("taskdef", 5), ("taskdef", ["review"]), ("direction", 5),
    ("direction", {"text": "hi"}), ("repo_url", 5), ("session_id", 5),
    ("session_id", ["20260824-165230-2-31245"]),
])
def test_a_non_string_option_is_refused(config, stopped_run, field, value):
    with pytest.raises(resume_mod.ResumeError, match="must be a string") as caught:
        resume_mod.resume_run(config, request_for(**{field: value}))
    assert caught.value.status == 400


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_blank_taskdef_means_the_recorded_one(config, stopped_run, blank):
    """An empty text box is "I did not say", not "I said nothing"."""
    result = resume_mod.resume_run(config, request_for(taskdef=blank))
    assert (result.continued, result.taskdef) == (True, TASKDEF)


def test_a_blank_repo_url_falls_back_to_the_recorded_one(config, stopped_run):
    assert resume_mod.resume_run(config, request_for(repo_url="  ")).continued is True


# --- the session is the unit of record (issue #353, D22) ---------------------
#
# In schema form a launch owns ``<project>/_runs/<slug>/<session-id>[--<name>]/``
# and ``_runs/<slug>/`` merely groups the launches against one (taskdef, target).
# So the slug an operator resumes can name several complete, independent records,
# and the two questions this section answers are "which one did the platform
# read" and "how does a caller say which one it meant".
#
# The resolve rule is not this module's to invent: ``workrepo._sessions_in`` is
# the one definition of "which sessions are records, newest last", used by the
# adoption picker and by ``resolve_run_dir`` alike. A resume that opened a
# different session than the picker offered is exactly the failure that shared
# rule exists to prevent, so the test below pins the two against each other
# rather than restating the rule.


def session_address(slug=None, session_dir=SESSION_ID):
    """The mirror-relative address of one planted session dir."""
    slug = slug if slug is not None else run_state.derive_slug(TASKDEF, TARGET)
    return f"{HOST}/{PROJECT}/_runs/{slug}/{session_dir}"


def test_a_schema_form_run_is_resumed_from_its_session_dir(
    config, resume_recording_lmer
):
    """The ordinary schema-form continue: one launch, one record, one address."""
    _script, dump = resume_recording_lmer
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    tracked = track_run()

    result = resume_mod.resume_run(config, request_for())

    assert result.continued is True
    assert result.started_slug == tracked.slug
    assert result.rel_path == session_address()
    assert result.to_dict()["rel_path"] == session_address()
    payload = child_payload(dump)
    assert (payload["task"], payload["target"]) == (TASKDEF, [TARGET])


def test_a_plain_slug_resume_reads_the_session_the_picker_offers(config):
    """Two launches, no session id: the resolve rule picks, and it is the shared
    one — a resume that read another session than the fleet row quotes would be
    the divergence ``_sessions_in`` exists to stop."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    plant_run(config, session_id=LATER_SESSION_ID, stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(config, request_for())

    offered = workrepo.resolve_run_dir(config, HOST, PROJECT, result.slug)
    assert result.rel_path == offered.rel_path
    assert result.rel_path == session_address(session_dir=LATER_SESSION_ID)


def test_a_session_id_continues_that_launch_and_its_state_governs(config):
    """The point of the field: the newest launch is finished and would need a
    direction, and the named one is not — so a resume that took the newest anyway
    would refuse a request that is perfectly resumable."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    plant_run(config, session_id=LATER_SESSION_ID, status="complete")
    track_run()

    result = resume_mod.resume_run(config, request_for(session_id=SESSION_ID))

    assert result.continued is True
    assert result.rel_path == session_address()


def test_the_newest_session_still_governs_when_no_session_is_named(config):
    """The other half of the same tree: without the field the finished launch is
    the one read, and its refusal names it."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    plant_run(config, session_id=LATER_SESSION_ID, status="complete")
    track_run()

    with pytest.raises(resume_mod.DirectionRequired) as caught:
        resume_mod.resume_run(config, request_for())

    assert session_address(session_dir=LATER_SESSION_ID) in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


def test_a_session_id_finds_the_dir_the_naming_rename_moved_it_to(config):
    """``work name`` renames the SESSION dir (``<sid>--<name>``) while the id it
    was launched under is what an operator has written down."""
    plant_run(config, session_id=f"{SESSION_ID}--nice-name", stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(config, request_for(session_id=SESSION_ID))

    assert result.rel_path == session_address(
        session_dir=f"{SESSION_ID}--nice-name"
    )
    assert result.continued is True


def test_the_full_session_dir_name_addresses_it_too(config):
    """It is what the fleet row shows, so it has to be a thing you can paste back."""
    plant_run(config, session_id=f"{SESSION_ID}--nice-name", stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(
        config, request_for(session_id=f"{SESSION_ID}--nice-name")
    )
    assert result.rel_path == session_address(
        session_dir=f"{SESSION_ID}--nice-name"
    )


def test_an_unknown_session_id_is_refused_and_says_what_is_there(config):
    """The remedy is a different session id, so the refusal has to carry the list —
    and a code, because a UI turns this one into a picker."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    plant_run(config, session_id=LATER_SESSION_ID, stop_reason="yield")
    track_run()

    with pytest.raises(resume_mod.SessionNotFound) as caught:
        resume_mod.resume_run(
            config, request_for(session_id="20260101-000000-2-1")
        )

    message = str(caught.value)
    assert (caught.value.status, caught.value.code) == (409, "session_not_found")
    assert SESSION_ID in message and LATER_SESSION_ID in message
    assert f"{HOST}/{PROJECT}/_runs/{request_for().slug}" in message
    assert registry.list_sessions(live_only=False) == []


def test_a_session_id_naming_two_dirs_is_refused_rather_than_guessed(config):
    """The name half of a session dir is unconstrained, so one id can end up on
    two directories — and a whole record is not worth a coin toss."""
    plant_run(config, session_id=f"{SESSION_ID}--one", stop_reason="yield")
    plant_run(config, session_id=f"{SESSION_ID}--two", stop_reason="yield")
    track_run()

    with pytest.raises(resume_mod.SessionNotFound, match="more than one") as caught:
        resume_mod.resume_run(config, request_for(session_id=SESSION_ID))

    assert f"{SESSION_ID}--one" in str(caught.value)
    assert f"{SESSION_ID}--two" in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


def test_a_session_id_against_a_legacy_run_is_refused(config):
    """A legacy tree keeps one shared dir per target and is read in place (D22),
    so there is no launch to address there — and the refusal names the dir that
    actually holds the record rather than the one the request implies."""
    plant_run(config, stop_reason="yield")
    track_run()

    with pytest.raises(resume_mod.SessionNotFound, match="legacy") as caught:
        resume_mod.resume_run(config, request_for(session_id=SESSION_ID))

    assert f"{HOST}/{PROJECT}/runs/{request_for().slug}" in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


def test_a_session_dir_with_no_state_file_is_not_a_session(config):
    """The one rule for "this is a record" is unchanged: it holds a state file."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()
    (config.mirror_path / HOST / PROJECT / "_runs" / request_for().slug
     / LATER_SESSION_ID).mkdir(parents=True)

    with pytest.raises(resume_mod.SessionNotFound):
        resume_mod.resume_run(config, request_for(session_id=LATER_SESSION_ID))


@pytest.mark.parametrize("session_id", [
    "../../../etc", "..", ".", "a/b", "..\\..\\windows", f"{SESSION_ID}/..",
])
def test_a_session_id_that_would_escape_the_mirror_is_refused(config, session_id):
    """It becomes a path segment under the run's grouping dir like any other."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()

    with pytest.raises(resume_mod.ResumeError, match="path") as caught:
        resume_mod.resume_run(config, request_for(session_id=session_id))
    assert caught.value.status == 400
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t "])
def test_a_blank_session_id_means_the_resolve_rule(config, blank):
    """The field is a text box the ordinary continue submits empty."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    plant_run(config, session_id=LATER_SESSION_ID, stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(config, request_for(session_id=blank))
    assert result.rel_path == session_address(session_dir=LATER_SESSION_ID)


def test_a_question_open_in_a_session_names_that_session_dir(config):
    """Every refusal quotes the address that was found, never a composed
    ``runs/<slug>`` — which in schema form is a directory that does not exist."""
    plant_run(config, session_id=SESSION_ID, stop_reason="question",
              question=QUESTION)
    track_run()

    with pytest.raises(resume_mod.QuestionOpen) as caught:
        resume_mod.resume_run(config, request_for())

    message = str(caught.value)
    assert message.startswith(f"{session_address()} is stopped on a question")
    assert f"{HOST}/{PROJECT}/runs/" not in message
    assert registry.list_sessions(live_only=False) == []


def test_an_unreadable_session_record_is_refused_by_its_address(config):
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()
    (config.mirror_path / HOST / PROJECT / "_runs" / request_for().slug
     / SESSION_ID / "state.yaml").write_text("not: [a, mapping\n", encoding="utf-8")

    with pytest.raises(resume_mod.NotResumable, match="could not be read") as caught:
        resume_mod.resume_run(config, request_for())
    assert str(caught.value).startswith(session_address())


def test_a_session_resume_writes_nothing_into_the_mirror(config):
    """Spec D3, in the shape where the platform now has one level more to walk."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    plant_run(config, session_id=LATER_SESSION_ID, stop_reason="yield")
    track_run()
    before = snapshot_tree(config.mirror_path)
    assert before

    resume_mod.resume_run(config, request_for(session_id=SESSION_ID))

    assert snapshot_tree(config.mirror_path) == before


def test_the_started_session_is_still_what_the_index_records(config, monkeypatch):
    """Two things are called a session here and only one of them is an index
    entry: ``last_session_id`` is the container session the spawn started
    (``s-…``), never the record dir a resume continued (``20260824-…``)."""
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    tracked = track_run()

    result = resume_mod.resume_run(config, request_for(session_id=SESSION_ID))
    try:
        entry = runs.get_tracked(HOST, PROJECT, tracked.slug)
        assert entry.last_session_id == result.session.session_id
        assert entry.last_session_id != SESSION_ID
        assert registry.read_session(result.session.session_id)["run"] == {
            "host": HOST, "project": PROJECT, "slug": tracked.slug,
        }
    finally:
        os.kill(result.session.pid, 9)


def test_a_sibling_that_does_not_exist_yet_has_no_address(config):
    """``rel_path`` is the record the checks were made against; an override that
    founds a run has none until the session it starts pushes one."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(config, request_for(taskdef="review"))

    assert result.continued is False
    assert result.rel_path is None
    assert result.to_dict()["rel_path"] is None


def test_an_existing_sibling_in_schema_form_is_addressed_by_its_session(config):
    """And when it does exist, the address is the sibling's session dir — the
    record whose state governed the checks."""
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()
    plant_run(config, taskdef="review", session_id=LATER_SESSION_ID)

    result = resume_mod.resume_run(config, request_for(taskdef="review"))

    assert result.rel_path == session_address(
        slug=run_state.derive_slug("review", TARGET),
        session_dir=LATER_SESSION_ID,
    )


# --- the record the session lands in (issue #353, D22) -----------------------
#
# Reading the right record is half of a resume; landing in it is the other half.
# The container mints ``LMER_SESSION_ID`` at startup only when the host injected
# none (``clone_and_exec.main`` -> ``mint_session_id``, ``os.environ.setdefault``),
# so a resume that names no record starts a BRAND NEW one beside the launch the
# operator asked to continue — the checks made against one record and the session
# written into another. What is pinned below is that the id the spawn carries is
# the id of the record every refusal above was measured against, and that it is
# carried only where that record is in the run the session will file itself under.


def session_flag_for(session_dir=SESSION_ID):
    """The one argv token that says which launch the session writes in."""
    return f"{spawn.SESSION_FLAG}={session_dir}"


def test_schema_two_unsigned_source_refusal_carries_launch_provenance(config):
    plant_schema_two_source(config)
    track_run()

    with pytest.raises(resume_mod.UnsignedSessionSource) as caught:
        resume_mod.resume_run(config, request_for(session_id="a" * 32))

    refusal = caught.value.to_dict()
    assert refusal == {
        "code": "unsigned_session",
        "message": str(caught.value),
        "source": {
            "uid": "a" * 32,
            "operator": "alice",
            "hostname": "build.example",
            "started_at": "2026-09-17T12:00:00Z",
            "last_activity": "2026-09-17T12:05:00Z",
        },
        "decisions": ["continue_fresh", "cancel"],
    }
    assert registry.list_sessions(live_only=False) == []


def test_schema_two_confirmation_starts_fresh_with_read_only_source(
    config, resume_recording_lmer
):
    _script, dump = resume_recording_lmer
    source = plant_schema_two_source(config)
    track_run()
    before = snapshot_tree(source)

    resume_mod.resume_run(config, request_for(
        session_id="a" * 32, allow_unsigned_source="a" * 32,
    ))

    payload = child_payload(dump)
    assert payload["session"] is None
    assert payload["source_session"] == "a" * 32
    assert payload["allow_unsigned"] == "a" * 32
    assert payload["metadata"]["operator"]
    assert payload["metadata"]["hostname"]
    assert snapshot_tree(source) == before


def test_schema_two_signed_source_needs_no_confirmation(config, resume_recording_lmer):
    _script, dump = resume_recording_lmer
    plant_schema_two_source(config, signed_out=True)
    track_run()

    resume_mod.resume_run(config, request_for(session_id="a" * 32))

    payload = child_payload(dump)
    assert payload["source_session"] == "a" * 32
    assert payload["allow_unsigned"] in (None, "")
    assert payload["session"] is None


def test_a_continue_lands_in_the_record_it_checked(config, resume_recording_lmer):
    """The defect this closes: same slug, same group, a second record."""
    _script, dump = resume_recording_lmer
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(config, request_for())

    payload = child_payload(dump)
    assert session_flag_for() in payload["argv"], (
        "without the id the container mints a fresh one and the resume opens a "
        "new record beside the launch it just read"
    )
    assert spawn.SESSION_FLAG not in payload["argv"], (
        "a bare --session token means the id was passed as a separate argument"
    )
    assert payload["session"] == SESSION_ID, (
        "the id has to be one lmer's own parser recovers — the contract spans "
        "two packages, and this module's intentions are not the test"
    )
    assert payload["rest"] == [], (
        "an unrecognised flag lands in rest and reaches nothing but the "
        "container's own command line"
    )
    assert result.rel_path == session_address(), (
        "the id carried must be the record the checks were made against"
    )


def test_a_named_session_is_the_one_continued_not_the_newest(config,
                                                             resume_recording_lmer):
    """The whole purpose of ``session_id``: it picked which record was read, and
    it has to pick which record is written in too — otherwise a resume of the
    older launch checks it and then lands somewhere it never looked."""
    _script, dump = resume_recording_lmer
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    plant_run(config, session_id=LATER_SESSION_ID, stop_reason="yield")
    track_run()

    resume_mod.resume_run(config, request_for(session_id=SESSION_ID))

    argv = child_payload(dump)["argv"]
    assert session_flag_for() in argv
    assert session_flag_for(LATER_SESSION_ID) not in argv


def test_the_id_carried_is_the_launch_and_not_the_named_dir(config,
                                                            resume_recording_lmer):
    """``<sid>`` and ``<sid>--<name>`` are the same session and the id is
    ``<sid>``: carrying the dir name would have the container compose
    ``<sid>--<name>--<name>`` the next time anything built the address from it."""
    _script, dump = resume_recording_lmer
    plant_run(config, session_id=f"{SESSION_ID}--nice-name", stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(config, request_for())

    assert result.rel_path == session_address(session_dir=f"{SESSION_ID}--nice-name")
    argv = child_payload(dump)["argv"]
    assert session_flag_for() in argv
    assert session_flag_for(f"{SESSION_ID}--nice-name") not in argv


def test_an_override_carries_no_session_id(config, resume_recording_lmer):
    """The sibling case, and the reason the injection is conditional: the session
    lands under a DIFFERENT slug, where the source record's id names nothing — so
    injecting it would create a record in one group named for a launch of another.
    The container mints its own, exactly as it did before this existed."""
    _script, dump = resume_recording_lmer
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()

    result = resume_mod.resume_run(config, request_for(taskdef="review"))

    assert result.continued is False
    assert not any(
        arg.startswith(spawn.SESSION_FLAG) for arg in child_payload(dump)["argv"]
    )


def test_an_override_onto_an_existing_sibling_carries_no_session_id(
    config, resume_recording_lmer
):
    """Even when the sibling has records of its own. The checks were made against
    the sibling's newest launch, but the operator asked to start work rather than
    to continue that launch — and ``_landing_slug`` says this is not a continue."""
    _script, dump = resume_recording_lmer
    plant_run(config, session_id=SESSION_ID, stop_reason="yield")
    track_run()
    plant_run(config, taskdef="review", session_id=LATER_SESSION_ID)

    result = resume_mod.resume_run(config, request_for(taskdef="review"))

    assert result.continued is False
    assert not any(
        arg.startswith(spawn.SESSION_FLAG) for arg in child_payload(dump)["argv"]
    )


def test_a_legacy_tree_resume_is_byte_identical_to_what_it_was(
    config, stopped_run, resume_recording_lmer
):
    """A legacy run dir is one shared record per target, read in place: nothing
    there is addressed by a launch, so there is no id to carry and the argv must
    not gain one."""
    _script, dump = resume_recording_lmer
    resume_mod.resume_run(config, request_for())

    assert not any(
        arg.startswith(spawn.SESSION_FLAG) for arg in child_payload(dump)["argv"]
    )


# --- the spawn path's own rules still apply ----------------------------------

def test_the_capacity_cap_still_applies_to_a_resume(platform_root, fake_lmer):
    """Continuing a run is not a reason to exceed the cap — it is a spawn."""
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    plant_run(config)
    track_run()
    registry.register("s-occupant", pid=os.getpid())

    with pytest.raises(spawn.CapacityError, match="1/1"):
        resume_mod.resume_run(config, request_for())


def test_a_failed_index_write_does_not_report_a_failed_resume(
    config, stopped_run, monkeypatch, caplog
):
    """The session is already running by then, so this must not raise.

    Reporting it as failed would send the operator to resume again, which the
    live-session check then refuses — two contradictory errors for a session that
    did start.
    """
    def unwritable(*_args, **_kwargs):
        raise store.StoreError("cannot write runs.json (disk full)")

    monkeypatch.setattr(runs, "note_session", unwritable)
    result = resume_mod.resume_run(config, request_for())

    assert result.session.session_id
    assert any(
        "platform_resume_index_not_updated" in r.message for r in caplog.records
    )


# --- the shape a route depends on --------------------------------------------

def test_the_reply_carries_the_spawns_warning_key(config, stopped_run):
    """``POST /api/sessions`` answers with a ``warning`` and so does this reply.

    A client should not have to know which of the two spawning routes omits the key.
    ``None`` on every path a resume can reach, and that is a property rather than an
    oversight: ``_repo_url_for`` refuses unless the URL parses back to this run's own
    host and project, which is exactly the derivation a warning would report having
    failed. So what is pinned here is the key and the pass-through, not a value the
    resume path can produce.
    """
    result = resume_mod.resume_run(config, request_for())
    payload = result.to_dict()

    assert "warning" in payload
    assert payload["warning"] == result.session.warning
    assert payload["warning"] is None, (
        "a resume supplies a repo URL that parses back to this run, so the spawn "
        "always has an identity to file the session under"
    )


@pytest.mark.parametrize("error,status,code", [
    (resume_mod.ResumeError, 400, "resume_refused"),
    (resume_mod.RunNotTracked, 404, "run_not_tracked"),
    (resume_mod.RepoUrlRequired, 400, "repo_url_required"),
    (resume_mod.DirectionRequired, 400, "direction_required"),
    (resume_mod.NotResumable, 409, "not_resumable"),
    (resume_mod.RunIsLive, 409, "live_session"),
    (resume_mod.QuestionOpen, 409, "question_open"),
    (resume_mod.SessionNotFound, 409, "session_not_found"),
])
def test_every_refusal_carries_a_status_and_a_stable_code(error, status, code):
    """The route maps the status; the UI branches on the code. Both are contract."""
    instance = error("because")
    assert isinstance(instance, resume_mod.ResumeError)
    assert (instance.status, instance.code) == (status, code)
    assert instance.to_dict() == {"code": code, "message": "because"}


def test_the_codes_are_unique():
    """Two refusals sharing a code is a UI that cannot tell them apart."""
    codes = [
        cls.code for cls in (
            resume_mod.ResumeError, resume_mod.RunNotTracked,
            resume_mod.RepoUrlRequired, resume_mod.DirectionRequired,
            resume_mod.NotResumable, resume_mod.RunIsLive, resume_mod.QuestionOpen,
            resume_mod.SessionNotFound, resume_mod.RunUnresolved,
            resume_mod.UnsignedSessionSource,
        )
    ]
    assert len(set(codes)) == len(codes)


def test_a_row_keyed_on_the_secondary_is_not_a_dead_end(config):
    """The row the fold cannot reach, and used to refuse forever (!269 review).

    A row keyed on the SECONDARY needs no old build to exist: `POST
    /api/runs/adopt` takes host and project verbatim with no registry
    validation, and the secondary is the URL an operator actually cloned from,
    so it is the spelling they would type. A spawn whose fold no-opped on a
    momentarily unreadable mirror leaves one too — the fold is best-effort by
    design, and this is what that costs.

    Folding only the URL made such a row unresumable *permanently*, and the
    refusal named a remedy that does not exist: every spelling of the project
    folds to the same primary, so no `repo_url` the operator could supply would
    ever satisfy "supply a repo_url for <secondary>".
    """
    plant_widgets_manifest(config)
    slug = run_state.derive_slug(TASKDEF, WIDGETS_TARGET)
    plant_widgets_run(config, slug)
    secondary_host, secondary_project = WIDGETS_SECONDARY.split("/", 1)
    runs.track(secondary_host, secondary_project, slug, source="adopted",
               taskdef=TASKDEF, target=WIDGETS_TARGET, repo=WIDGETS_URL)

    result = resume_mod.resume_run(config, resume_mod.ResumeRequest(
        host=secondary_host, project=secondary_project, slug=slug,
    ))

    assert result.session.session_id, (
        "a run tracked under one of its own remotes was refused a resume, with "
        "advice no URL could satisfy"
    )


def test_a_repo_url_naming_another_project_is_still_refused(config):
    """The converse, so folding both sides is not a widening: the check still
    refuses a URL that names a genuinely different project."""
    plant_widgets_manifest(config)
    slug = run_state.derive_slug(TASKDEF, WIDGETS_TARGET)
    plant_widgets_run(config, slug)
    secondary_host, secondary_project = WIDGETS_SECONDARY.split("/", 1)
    runs.track(secondary_host, secondary_project, slug, source="adopted",
               taskdef=TASKDEF, target=WIDGETS_TARGET, repo=WIDGETS_URL)

    with pytest.raises(resume_mod.RepoUrlRequired, match="names"):
        resume_mod.resume_run(config, resume_mod.ResumeRequest(
            host=secondary_host, project=secondary_project, slug=slug,
            repo_url="https://gitlab.example.com/acme/gadgets",
        ))


# --- schema 2: a record is one execution, addressed by its uid (spec §6.4) ---
#
# In a schema-2 tree the identity is ``host/project/<uid>`` and the slug is a
# group label. A continuation never writes in its source: the spawn mints a
# fresh uid (``--launch-uid``) and the source rides as read-only context
# (``--source-session``). So the slug-era rules — the landing prediction, the
# live refusal, ``note_session`` on the source row, and "records no target" —
# are lifted for a record carrying ``session.uid``, and every legacy test above
# pins that they still hold everywhere else. The recording stub's ``repo`` and
# ``launch_uid`` are what ``lmer``'s own parser recovers for that pair.

UID = "0123456789abcdef" * 2
OTHER_UID = "fedcba9876543210" * 2
DOC_TARGET = "https://gitlab.example.com/agents/global/-/blob/main/README.md"
SSH_HOST = "git.example.test:2222"
SSH_PROJECT = "acme/widgets"
SSH_URL = "ssh://git@git.example.test:2222/acme/widgets.git"


def plant_schema_two_manifest(config, projects="{}"):
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        f"schema: 2\nname: example-plexus\nprojects: {projects}\n", encoding="utf-8"
    )


def plant_execution(
    config,
    uid=UID,
    *,
    base=None,
    taskdef=TASKDEF,
    target=TARGET,
    status="in-progress",
    stop_reason="yield",
    question=None,
    events=("session_start", "session_end"),
    seeded_by=None,
):
    """One schema-2 record: ``<project dir>/_runs/<slug>/<uid>/`` whose state
    carries ``session.uid`` — the shape spec §6.4 lifts the slug-era rules for."""
    slug = run_state.derive_slug(taskdef, target or "")
    base = base if base is not None else config.mirror_path / HOST / PROJECT
    path = base / "_runs" / slug / uid
    path.mkdir(parents=True, exist_ok=True)
    lines = [
        "schema: 2",
        f"status: {json.dumps(status)}",
        f"slug: {json.dumps(slug)}",
        f"taskdef: {json.dumps(taskdef)}",
        f"target: {json.dumps(target or '')}",
    ]
    for name, value in (("stop_reason", stop_reason), ("open_question", question)):
        if value is not None:
            lines.append(f"{name}: {json.dumps(value)}")
    lines += [
        "session:",
        f"  uid: {uid}",
        "  operator: alice",
        "  hostname: build.example",
        "  started_at: 2026-09-17T12:00:00Z",
    ]
    if seeded_by is not None:
        lines.append(f"  seeded_by: {seeded_by}")
    (path / "state.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "events.jsonl").write_text(
        "".join(
            json.dumps({"ts": "2026-09-17T12:00:00Z", "type": kind}) + "\n"
            for kind in ("run_seeded", *events)
        ),
        encoding="utf-8",
    )
    return path


def track_execution(uid=UID, *, host=HOST, project=PROJECT, taskdef=TASKDEF,
                    target=TARGET, repo=REPO, session_id=None, source="spawned"):
    return runs.track(
        host, project, run_state.derive_slug(taskdef, target or ""), uid=uid,
        source=source, taskdef=taskdef, target=target or None, repo=repo,
        session_id=session_id,
    )


def uid_request(uid=UID, **fields):
    fields.setdefault("host", HOST)
    fields.setdefault("project", PROJECT)
    return resume_mod.ResumeRequest(uid=uid, **fields)


def spawned_argv(dump):
    """The argv the stub recorded, less the spawn's per-session ``--mount-dir``
    pairs — paths under this test's tmp dir that are the spawn's own plumbing,
    identical for every launch and not what a continuation decides."""
    argv, kept = child_payload(dump)["argv"], []
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--mount-dir":
            skip = True
        else:
            kept.append(arg)
    return kept


def launch_argv(*head, repo=None, source=UID, launch_uid, tail=()):
    """The exact ``lmer`` argv a schema-2 continuation spawns (spec §6.4)."""
    argv = [*head, "--fastapi", spawn.NO_THREAD_CONNECTORS_FLAG]
    if repo is not None:
        argv.append(f"{spawn.REPO_FLAG}={repo}")
    argv.append(f"{spawn.SOURCE_SESSION_FLAG}={source}")
    argv += list(tail)
    argv.append(f"{spawn.LAUNCH_UID_FLAG}={launch_uid}")
    return argv


def test_a_uid_addressed_resume_needs_no_slug(config, resume_recording_lmer):
    """The tracked entry supplies the group label; the uid is the address."""
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(config)
    plant_execution(config)
    tracked = track_execution()

    result = resume_mod.resume_run(config, uid_request())

    payload = child_payload(dump)
    new_uid = result.session.uid
    assert new_uid and new_uid != UID, "a continuation is a fresh execution"
    assert spawned_argv(dump) == launch_argv(TASKDEF, TARGET, launch_uid=new_uid)
    assert payload["launch_uid"] == new_uid
    assert payload["session"] is None, "session_id is never set for schema 2"
    assert payload["source_session"] == UID
    assert result.slug == tracked.slug
    reply = result.to_dict()
    assert reply["run"]["uid"] == UID
    assert reply["started"]["uid"] == new_uid


@pytest.mark.parametrize("uid", ["A" * 32, "a" * 31, "g" * 32, "a" * 33, 5])
def test_a_uid_that_is_not_32_lowercase_hex_is_refused(config, uid):
    with pytest.raises(resume_mod.ResumeError, match="uid") as caught:
        resume_mod.resume_run(config, uid_request(uid=uid))
    assert caught.value.status == 400
    assert registry.list_sessions(live_only=False) == []


def test_a_uid_with_a_session_id_naming_another_record_is_refused(config):
    """Both pick the record a resume reads; quietly preferring one reads a whole
    record off a launch the caller did not mean."""
    plant_schema_two_manifest(config)
    plant_execution(config)
    track_execution()

    with pytest.raises(resume_mod.ResumeError, match="different record") as caught:
        resume_mod.resume_run(config, uid_request(session_id=OTHER_UID))
    assert caught.value.status == 400
    assert registry.list_sessions(live_only=False) == []


def test_a_record_whose_uid_is_not_its_directorys_is_refused(config):
    """A copied record: the directory addressed is ``UID`` while the state says
    ``OTHER_UID``. Launching would pass one as ``--source-session`` while the
    reply and the event named the other."""
    plant_schema_two_manifest(config)
    path = plant_execution(config)
    state = path / "state.yaml"
    state.write_text(state.read_text(encoding="utf-8").replace(
        f"  uid: {UID}", f"  uid: {OTHER_UID}"), encoding="utf-8")
    track_execution()

    with pytest.raises(resume_mod.NotResumable, match=OTHER_UID) as caught:
        resume_mod.resume_run(config, uid_request())

    assert UID in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


def test_a_seeded_source_continues_without_an_acknowledgement(
    config, resume_recording_lmer
):
    """``work seed`` publishes a record nobody has started in (spec §5.2, D13):
    nothing can be writing it, so it is a source with no unsigned decision."""
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(config)
    plant_execution(config, events=(), seeded_by=OTHER_UID)
    track_execution()

    result = resume_mod.resume_run(config, uid_request())

    payload = child_payload(dump)
    assert spawned_argv(dump) == launch_argv(
        TASKDEF, TARGET, launch_uid=result.session.uid
    )
    assert payload["allow_unsigned"] in (None, "")


def test_a_started_source_without_a_sign_out_still_needs_one(config):
    """The contrast: ``seeded_by`` alone is history once a session started."""
    plant_schema_two_manifest(config)
    plant_execution(config, events=("session_start",), seeded_by=OTHER_UID)
    track_execution()

    with pytest.raises(resume_mod.UnsignedSessionSource):
        resume_mod.resume_run(config, uid_request())
    assert registry.list_sessions(live_only=False) == []


def test_two_continuations_of_one_source_both_start(
    config, resume_recording_lmer, monkeypatch
):
    """A fork is two rows with one label (D8): nothing refuses the second, and
    its reply names the first instead (#311)."""
    plant_schema_two_manifest(config)
    plant_execution(config)
    track_execution()
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")

    first = resume_mod.resume_run(config, uid_request())
    pids = [first.session.pid]
    try:
        assert wait_for(
            lambda: registry.is_live(registry.read_session(first.session.session_id))
        )
        second = resume_mod.resume_run(config, uid_request())
        pids.append(second.session.pid)

        assert first.to_dict()["warning"] is None
        warning = second.to_dict()["warning"]
        assert warning and first.session.session_id in warning
        assert "live session(s) already on" in warning
        assert first.session.uid != second.session.uid
        assert len(registry.list_sessions(live_only=True)) == 2
    finally:
        for pid in pids:
            os.kill(pid, 9)


def test_an_unresolved_entry_is_refused_with_its_candidates(config):
    """A slug row the key transition could not bind is never read as its
    group's newest record — the refusal lists what to re-adopt by uid."""
    plant_schema_two_manifest(config)
    plant_execution(config, UID)
    plant_execution(config, OTHER_UID)
    tracked = track_run()
    candidates = [
        {"uid": UID, "started_at": "2026-09-17T12:00:00Z", "name": None},
        {"uid": OTHER_UID, "started_at": "2026-09-18T12:00:00Z", "name": "b"},
    ]
    assert runs.mark_unresolved(tracked.key, candidates)

    with pytest.raises(resume_mod.RunUnresolved) as caught:
        resume_mod.resume_run(config, request_for())

    assert (caught.value.status, caught.value.code) == (409, "run_unresolved")
    assert caught.value.to_dict() == {
        "code": "run_unresolved",
        "message": str(caught.value),
        "candidates": candidates,
    }
    assert UID in str(caught.value) and OTHER_UID in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


def test_the_source_row_keeps_its_session_and_the_continuation_is_its_own_row(
    config,
):
    """#229 for schema-2 rows: the source reads from its own record, never from
    a ``last_session_id`` pointed at somebody else's session."""
    plant_schema_two_manifest(config)
    plant_execution(config)
    track_execution(session_id="s-source")

    result = resume_mod.resume_run(config, uid_request())

    source = runs.get_tracked(HOST, PROJECT, uid=UID)
    assert source.last_session_id == "s-source"
    continuation = runs.get_tracked(HOST, PROJECT, uid=result.session.uid)
    assert continuation is not None
    assert continuation.key == f"{HOST}/{PROJECT}/{result.session.uid}"
    assert continuation.last_session_id == result.session.session_id


def test_a_live_session_on_the_label_does_not_refuse_a_schema_two_continue(
    config, resume_recording_lmer
):
    """The legacy ``RunIsLive`` keyed on the slug; here the slug is a label two
    executions share, and the live one is reported, not obeyed."""
    plant_schema_two_manifest(config)
    plant_execution(config)
    tracked = track_execution()
    registry.register(
        "s-holder", pid=os.getpid(), task={"taskdef": TASKDEF, "target": TARGET},
        run={"host": HOST, "project": PROJECT, "slug": tracked.slug, "uid": UID},
    )

    result = resume_mod.resume_run(config, uid_request())

    assert "s-holder" in result.to_dict()["warning"]


def test_a_finished_schema_two_record_is_continued_as_a_new_execution(config):
    """#437's platform site: the refusal names the route that works on schema 2
    — a new execution with the record as its source — not a reopen."""
    plant_schema_two_manifest(config)
    plant_execution(config, status="complete", stop_reason=None)
    track_execution()

    with pytest.raises(resume_mod.DirectionRequired) as caught:
        resume_mod.resume_run(config, uid_request())

    message = str(caught.value)
    assert "new execution" in message
    assert f"`lmer {TASKDEF} {TARGET} --source-session {UID}`" in message
    assert "reopens the run" not in message
    assert registry.list_sessions(live_only=False) == []


def test_the_continue_line_quotes_a_recorded_target_scrubbed(config):
    """Review !387 round 3, discussion 11: the refusal reaches the UI and the
    assistant's transcript, so a credential in the recorded target does not."""
    token_target = (
        "https://oauth2:glpat-s3cr3t@gitlab.example.com/agents/global/-/wikis/Home"
    )
    plant_schema_two_manifest(config)
    plant_execution(config, taskdef="chat", target=token_target,
                    status="complete", stop_reason=None)
    track_execution(taskdef="chat", target=token_target)

    with pytest.raises(resume_mod.DirectionRequired) as caught:
        resume_mod.resume_run(config, uid_request())

    assert "glpat-s3cr3t" not in str(caught.value)
    assert "glpat-s3cr3t" not in json.dumps(caught.value.to_dict())
    assert "gitlab.example.com/agents/global/-/wikis/Home" in str(caught.value)
    assert f"--source-session {UID}" in str(caught.value)


def test_a_legacy_finished_run_keeps_the_reopen_wording(config):
    plant_run(config, status="complete")
    track_run()

    with pytest.raises(resume_mod.DirectionRequired, match="reopens the run") as caught:
        resume_mod.resume_run(config, request_for())
    assert "--source-session" not in str(caught.value)


# --- schema 2: an empty-target record launches on a transport URL (#537) -----
#
# astra's R5 fixtures. The transport is the URL ``--repo`` clones from, so it is
# only ever the caller's ``repo_url`` or the run's recorded clone URL. The
# registry's scheme-less ``repo:`` and the primary-remote fold are identity:
# they validate the URL and are never cloned from.


def test_an_empty_target_record_resumes_with_the_transport_url(
    config, resume_recording_lmer
):
    """A caller-supplied URL, for a run that recorded none."""
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(config)
    plant_execution(config, taskdef="chat", target="")
    track_execution(taskdef="chat", target="", repo=None, source="adopted")

    result = resume_mod.resume_run(config, uid_request(repo_url=REPO))

    payload = child_payload(dump)
    assert spawned_argv(dump) == launch_argv(
        "chat", repo=REPO, launch_uid=result.session.uid
    )
    assert not payload["target"], "no empty positional for argparse to take"
    assert payload["repo"] == REPO
    assert payload["session"] is None
    assert result.target == ""
    assert result.started_slug == "chat"


def test_the_recorded_clone_url_is_the_transport_when_none_is_supplied(
    config, resume_recording_lmer
):
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(config)
    plant_execution(config, taskdef="chat", target="")
    track_execution(taskdef="chat", target="", repo=REPO)

    result = resume_mod.resume_run(config, uid_request())

    assert spawned_argv(dump) == launch_argv(
        "chat", repo=REPO, launch_uid=result.session.uid
    )


def test_a_canonical_only_registry_is_never_a_clone_url(config):
    """The registry knows the project's identity and nothing about how to clone
    it, so with no recorded or supplied URL the platform asks."""
    plant_schema_two_manifest(
        config, projects=f"\n  widgets:\n    repo: {WIDGETS_PRIMARY}"
    )
    host, project = WIDGETS_PRIMARY.split("/", 1)
    plant_execution(
        config, base=config.mirror_path / "widgets", taskdef="chat", target=""
    )
    track_execution(host=host, project=project, taskdef="chat", target="",
                    repo=None, source="adopted")

    with pytest.raises(resume_mod.RepoUrlRequired) as caught:
        resume_mod.resume_run(config, uid_request(host=host, project=project))

    assert caught.value.code == "repo_url_required"
    assert "will not invent one" in str(caught.value)
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("supplied", [True, False], ids=["supplied", "recorded"])
def test_the_registrys_identity_spelling_is_refused_as_a_transport(config, supplied):
    """Review !387 round 3, discussion 12. ``gitlab.example.com/acme/widgets``
    folds to the run's own identity through the registry, but it is no clone
    URL: ``--repo`` would name no path or repository, and the spawn would have
    written it into the run's ``repo`` for every later verb to trust."""
    plant_schema_two_manifest(
        config, projects=f"\n  widgets:\n    repo: {WIDGETS_PRIMARY}"
    )
    host, project = WIDGETS_PRIMARY.split("/", 1)
    plant_execution(
        config, base=config.mirror_path / "widgets", taskdef="chat", target=""
    )
    track_execution(host=host, project=project, taskdef="chat", target="",
                    repo=None if supplied else WIDGETS_PRIMARY, source="adopted")
    request = uid_request(host=host, project=project,
                          **({"repo_url": WIDGETS_PRIMARY} if supplied else {}))

    with pytest.raises(resume_mod.RepoUrlRequired, match="not a clone URL") as caught:
        resume_mod.resume_run(config, request)

    assert caught.value.code == "repo_url_required"
    assert registry.list_sessions(live_only=False) == []
    assert runs.get_tracked(host, project, uid=UID).repo == (
        None if supplied else WIDGETS_PRIMARY
    )


def test_a_secondary_remote_recorded_as_the_runs_repo_is_the_transport(
    config, resume_recording_lmer
):
    """The run is keyed on the PRIMARY and was cloned through the secondary
    (D8): the recorded secondary is what the continuation clones from."""
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(
        config,
        projects=(
            f"\n  widgets:\n    repo: {WIDGETS_PRIMARY}\n"
            f"    remotes: [{WIDGETS_SECONDARY}]"
        ),
    )
    host, project = WIDGETS_PRIMARY.split("/", 1)
    plant_execution(
        config, base=config.mirror_path / "widgets", taskdef="chat", target=""
    )
    track_execution(host=host, project=project, taskdef="chat", target="",
                    repo=WIDGETS_URL)

    result = resume_mod.resume_run(config, uid_request(host=host, project=project))

    assert spawned_argv(dump) == launch_argv(
        "chat", repo=WIDGETS_URL, launch_uid=result.session.uid
    )
    assert (result.session.host, result.session.project) == (host, project)


def test_an_ssh_endpoint_on_a_non_default_port_is_carried_verbatim(
    config, resume_recording_lmer
):
    """Lossless endpoint identity (``host:port``) for an unregistered repo, and
    the URL itself — user, port and all — as the transport."""
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(config)
    bucket = workrepo.bucket_path(
        config.mirror_path, f"{SSH_HOST}/{SSH_PROJECT}", schema=2
    )
    plant_execution(config, base=bucket, taskdef="chat", target="")
    track_execution(host=SSH_HOST, project=SSH_PROJECT, taskdef="chat",
                    target="", repo=SSH_URL)

    result = resume_mod.resume_run(
        config, uid_request(host=SSH_HOST, project=SSH_PROJECT)
    )

    payload = child_payload(dump)
    assert spawned_argv(dump) == launch_argv(
        "chat", repo=SSH_URL, launch_uid=result.session.uid
    )
    assert payload["repo"] == SSH_URL
    assert (result.session.host, result.session.project) == (SSH_HOST, SSH_PROJECT)


def test_a_reference_target_keeps_its_place_and_clones_from_the_transport(
    config, resume_recording_lmer
):
    """#523: the document is the task's context, the repository is ``--repo``."""
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(config)
    plant_execution(config, taskdef="chat", target=DOC_TARGET)
    track_execution(taskdef="chat", target=DOC_TARGET, repo=REPO)

    result = resume_mod.resume_run(config, uid_request())

    assert spawned_argv(dump) == launch_argv(
        "chat", DOC_TARGET, repo=REPO, launch_uid=result.session.uid
    )


def test_a_transport_url_naming_another_project_is_refused_and_scrubbed(config):
    """Checked against the source's own project identity — and a transport URL
    may carry a credential, which no refusal repeats."""
    plant_schema_two_manifest(config)
    plant_execution(config, taskdef="chat", target="")
    track_execution(taskdef="chat", target="", repo=None, source="adopted")

    with pytest.raises(resume_mod.RepoUrlRequired, match="but the run is") as caught:
        resume_mod.resume_run(config, uid_request(
            repo_url="https://oauth2:s3cr3t-token@gitlab.example.com/someone/else.git"
        ))

    assert "s3cr3t-token" not in str(caught.value)
    assert "s3cr3t-token" not in json.dumps(caught.value.to_dict())
    assert registry.list_sessions(live_only=False) == []



#: A record whose target is a bare clone URL — written before the run-identity
#: rule moved a repository-class target into ``repo_url`` (spec §6.5).
REPO_TARGET = "https://gitlab.example.com/agents/global.git"


def test_a_repository_target_record_resumes_on_the_transport_url(
    config, resume_recording_lmer
):
    """Passing the recorded clone URL as the positional beside a ``repo_url``
    is "two repositories named" to the spawn, so such a record could never be
    resumed. ``derive_slug`` gives the bare taskdef either way, so dropping the
    positional for ``--repo`` lands in the same group."""
    _script, dump = resume_recording_lmer
    plant_schema_two_manifest(config)
    plant_execution(config, target=REPO_TARGET)
    track_execution(target=REPO_TARGET, repo=REPO)

    result = resume_mod.resume_run(config, uid_request())

    assert spawned_argv(dump) == launch_argv(
        TASKDEF, repo=REPO, launch_uid=result.session.uid
    )
    assert not child_payload(dump)["target"]
    assert result.started_slug == TASKDEF


def test_a_legacy_repository_target_record_resumes_on_the_repo_url(
    config, resume_recording_lmer
):
    _script, dump = resume_recording_lmer
    plant_run(config, target=REPO_TARGET, stop_reason="yield")
    track_run(target=REPO_TARGET)

    result = resume_mod.resume_run(config, request_for(slug=TASKDEF))

    payload = child_payload(dump)
    assert f"{spawn.REPO_FLAG}={REPO}" in payload["argv"]
    assert not payload["target"]
    assert result.started_slug == TASKDEF and result.continued is True


def test_a_schema_two_resume_event_names_both_executions(config):
    plant_schema_two_manifest(config)
    plant_execution(config)
    track_execution()

    result = resume_mod.resume_run(config, uid_request())

    resumed = [e for e in store.read_events() if e["type"] == "run_resumed"][-1]
    assert resumed["data"]["run"]["uid"] == UID
    assert resumed["data"]["started"]["uid"] == result.session.uid


def muted_at_registration(monkeypatch):
    """The run keys muted at the moment each session is registered — the point
    after which the daemon reads that session's records."""
    seen = []
    real = registry.register

    def register(session_id, **fields):
        seen.append(set(mute.load_all()))
        return real(session_id, **fields)

    monkeypatch.setattr(registry, "register", register)
    return seen


def test_a_schema_two_continue_carries_a_muted_runs_mute(config, monkeypatch):
    """A continue is a new uid-keyed row (issue 458) and stays muted — from
    before its session is published, so no record it writes is read unmuted."""
    plant_schema_two_manifest(config)
    plant_execution(config)
    tracked = track_execution()
    mute.set_run(tracked.host, tracked.project, uid=UID, muted=True)
    seen = muted_at_registration(monkeypatch)

    result = resume_mod.resume_run(config, uid_request())

    assert result.continued is True
    started = runs.run_key(tracked.host, tracked.project, result.session.uid)
    assert mute.is_muted((tracked.host, tracked.project, result.session.uid))
    assert seen and started in seen[0]


def test_a_schema_two_override_does_not_carry_the_mute(config):
    """An override starts a different run, which starts unmuted."""
    plant_schema_two_manifest(config)
    plant_execution(config)
    tracked = track_execution()
    mute.set_run(tracked.host, tracked.project, uid=UID, muted=True)

    result = resume_mod.resume_run(config, uid_request(taskdef="review"))

    assert result.continued is False
    assert not mute.is_muted((tracked.host, tracked.project, result.session.uid))
    assert mute.is_muted((tracked.host, tracked.project, UID))


def test_an_unmuted_continue_starts_unmuted(config):
    plant_schema_two_manifest(config)
    plant_execution(config)
    tracked = track_execution()

    result = resume_mod.resume_run(config, uid_request())

    assert mute.load_all() == {}
    assert not mute.is_muted((tracked.host, tracked.project, result.session.uid))


def test_resuming_a_muted_renamed_run_carries_the_mute_to_its_recorded_slug(
    config, monkeypatch
):
    """The resumed session registers under the recorded slug, which the spawn
    now tracks as a run too — and a tracked bare slug is never matched to its
    renamed neighbour by name. So the resume carries the mute there, before the
    session is published (issue 458)."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    renamed = f"{recorded}--nice-name"
    plant_run(config, slug=renamed, recorded_slug=recorded)
    track_run(slug=renamed)
    mute.set_run(HOST, PROJECT, renamed, muted=True)
    seen = muted_at_registration(monkeypatch)

    result = resume_mod.resume_run(config, request_for(slug=renamed))

    assert result.session.slug == recorded
    assert runs.get_tracked(HOST, PROJECT, recorded) is not None
    bare = runs.run_key(HOST, PROJECT, recorded)
    assert seen and bare in seen[0]
    assert mute.load_all()[bare]["carried_from"] == runs.run_key(HOST, PROJECT, renamed)
    block = {"host": HOST, "project": PROJECT, "slug": recorded}
    note = {"kind": "note", "text": "x", "source": {"channel": "agent"}}
    assert mute.suppressed(result.session.session_id, note, block) is True
    # And unmuting the run the operator muted lets that session speak again.
    mute.set_run(HOST, PROJECT, renamed, muted=False)
    assert mute.suppressed(result.session.session_id, note, block) is False


def test_unmuting_a_resumed_renamed_runs_live_row_survives_the_next_resume(
    config, monkeypatch
):
    """The UI's live-row toggle unmutes by session, which resolves to the bare
    key the resumed session runs under. That must take the renamed key's mute
    with it, or the next resume reads it and carries it back (issue 458)."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    renamed = f"{recorded}--nice-name"
    plant_run(config, slug=renamed, recorded_slug=recorded)
    track_run(slug=renamed)
    mute.set_run(HOST, PROJECT, renamed, muted=True)
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    first = resume_mod.resume_run(config, request_for(slug=renamed))
    try:
        assert mute.run_muted((HOST, PROJECT, recorded))
        client = TestClient(api.create_app(
            config, "test-secret-value",
            state_builder=lambda config, force_pull=False: {},
        ))
        reply = client.post(
            f"/api/sessions/{first.session.session_id}/unmute",
            headers={"Authorization": "Bearer test-secret-value"}, json={},
        )
        assert reply.status_code == 200, reply.text
        assert mute.load_all() == {}
        assert mute.source_of(HOST, PROJECT, renamed) is None
    finally:
        os.kill(first.session.pid, 9)
    assert wait_for(lambda: not registry.list_sessions(live_only=True))

    monkeypatch.setenv("FAKE_LMER_SLEEP", "0")
    second = resume_mod.resume_run(config, request_for(slug=renamed))

    assert second.continued is True
    assert mute.load_all() == {}
    note = {"kind": "note", "text": "x", "source": {"channel": "agent"}}
    block = {"host": HOST, "project": PROJECT, "slug": recorded}
    assert mute.suppressed(second.session.session_id, note, block) is False


def test_resuming_an_unmuted_renamed_run_carries_nothing(config):
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    renamed = f"{recorded}--nice-name"
    plant_run(config, slug=renamed, recorded_slug=recorded)
    track_run(slug=renamed)

    resume_mod.resume_run(config, request_for(slug=renamed))

    assert mute.load_all() == {}


def test_a_legacy_resume_event_keeps_its_shape(config, stopped_run):
    resume_mod.resume_run(config, request_for())

    resumed = [e for e in store.read_events() if e["type"] == "run_resumed"][-1]
    assert set(resumed["data"]["run"]) == {"host", "project", "slug"}
    assert set(resumed["data"]["started"]) == {"host", "project", "slug"}
