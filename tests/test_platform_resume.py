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

Four properties get the most attention:

- **continue vs sibling**: the recorded taskdef continues the run in front of the
  operator; an overridden one starts a different run against the same target, and
  the reply says which happened;
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

from lmer_platform import config as cfg
from lmer_platform import registry, resume as resume_mod, runs, spawn, store
from tests.conftest import strip_lmer_env
from work_repo import run_state

HOST = "gitlab.example.com"
PROJECT = "agents/global"
TASKDEF = "develop"
TARGET = "https://gitlab.example.com/agents/global/-/work_items/141"
REPO = "https://gitlab.example.com/agents/global"
QUESTION = "Should the queue survive a daemon restart, or start empty?"


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
    """
    dump = tmp_path / "resume.json"
    script = tmp_path / "resume-lmer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from lmer_cli.cli import parse_args\n"
        "ns, rest = parse_args(sys.argv[1:])\n"
        "payload = {'argv': sys.argv[1:], 'task': ns.task, 'target': ns.target,\n"
        "           'prompt': ns.prompt, 'answer': ns.answer, 'rest': rest}\n"
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
    extra="",
):
    """Write one run dir into the mirror the way a pushed run appears there.

    Written as YAML text rather than through ``run_state.write_state`` on purpose:
    the mirror is a *read* surface for the platform, and planting bytes is the only
    way a test can be sure nothing in the resume path wrote to it. Values go
    through ``json.dumps`` (valid YAML) so a target full of metacharacters is
    planted as the string it is rather than as accidental YAML.
    """
    slug = slug if slug is not None else run_state.derive_slug(taskdef, target)
    path = config.mirror_path / host / project / "runs" / slug
    path.mkdir(parents=True, exist_ok=True)
    lines = ["schema: 1", f"status: {json.dumps(status)}"]
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
    ("direction", {"text": "hi"}), ("repo_url", 5),
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
        )
    ]
    assert len(set(codes)) == len(codes)
