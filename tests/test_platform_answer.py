"""Tests for answering a question-blocked run (issue #141, slice M2 / T19).

The whole feature is "spawn the run again with the answer attached", so the tests
that matter are about what the *child* actually receives and about every case
where the platform must refuse instead of spawning something that quietly does
nothing.

Sessions are spawned for real against a stub standing in for ``lmer`` — the same
approach as tests/test_platform_spawn.py, extended so the stub records its own
``sys.argv`` and runs it through ``lmer``'s own parser. That is what makes these
assertions about the answer an operator would actually get rather than about a
string this module intended to build: argv is a list, not a shell string, and
``lmer`` is the thing that has to recover the text from it.

Three properties get the most attention here:

- **the answer survives**: quotes, newlines, shell metacharacters and a leading
  dash all come back out of ``lmer``'s argument parser unchanged;
- **the platform writes nothing to run state** (spec D3) — the mirror is byte-for-
  byte untouched by an answer;
- **every refusal is a refusal**, with the reason in the message, because a spawn
  that cannot deliver the answer is worse than an error.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from lmer_platform import answer as answer_mod
from lmer_platform import config as cfg
from lmer_platform import inventory, registry, resume as resume_mod, runs, spawn, store
from tests.conftest import strip_lmer_env
from work_repo import run_state

WEB = Path(__file__).resolve().parent.parent / "web"

HOST = "gitlab.example.com"
PROJECT = "agents/global"
TASKDEF = "develop"
TARGET = "https://gitlab.example.com/agents/global/-/work_items/141"
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
def answer_recording_lmer(tmp_path):
    """A stub that records its argv *and* what ``lmer``'s parser makes of it.

    Both halves are needed. The argv proves the platform passed one ``=`` token
    rather than two arguments (the two-token spelling loses any answer that starts
    with a dash — argparse reads the value as an option). The parsed value proves
    the text an operator typed is the text ``lmer`` would export as
    ``LMER_ANSWER``, which is the contract this feature actually depends on and
    which spans two packages.

    Runs under ``sys.executable`` so it imports the parser from the interpreter the
    suite is using, in the spirit of test_platform_spawn.py's ``resolving_lmer``.
    """
    dump = tmp_path / "answer.json"
    script = tmp_path / "answer-lmer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from lmer_cli.cli import parse_args\n"
        "ns, rest = parse_args(sys.argv[1:])\n"
        "payload = {'argv': sys.argv[1:], 'answer': ns.answer, 'task': ns.task,\n"
        "           'target': ns.target, 'rest': rest}\n"
        f"open({str(dump)!r}, 'w', encoding='utf-8').write(json.dumps(payload))\n"
        # Staying alive is how a test keeps the session's registry entry around; a
        # clean exit reaps it, which would race any assertion about a live one.
        "time.sleep(float(os.environ.get('FAKE_LMER_SLEEP') or 0))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, dump


@pytest.fixture
def fake_lmer(answer_recording_lmer):
    return answer_recording_lmer[0]


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
    stop_reason="question",
    question=QUESTION,
    status="in-progress",
    recorded_slug=...,
    extra="",
):
    """Write one run dir into the mirror the way a pushed run appears there.

    Written as YAML text rather than through ``run_state.write_state`` on purpose:
    the mirror is a *read* surface for the platform, and planting bytes is the only
    way a test can be sure nothing in the answer path wrote to it.
    """
    slug = slug if slug is not None else run_state.derive_slug(taskdef, target)
    path = config.mirror_path / host / project / "runs" / slug
    path.mkdir(parents=True, exist_ok=True)
    lines = ["schema: 1", f"status: {status}"]
    recorded = slug if recorded_slug is ... else recorded_slug
    if recorded is not None:
        lines.append(f"slug: {recorded}")
    if taskdef is not None:
        lines.append(f"taskdef: {taskdef}")
    if target is not None:
        lines.append(f"target: {target}")
    if stop_reason is not None:
        lines.append(f"stop_reason: {stop_reason}")
    if question is not None:
        lines.append(f"open_question: {json.dumps(question)}")
    if extra:
        lines.append(extra)
    (path / "state.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "events.jsonl").write_text(
        json.dumps({"ts": "2026-07-26T10:00:00Z", "type": "run_seeded"}) + "\n",
        encoding="utf-8",
    )
    return path


def track_run(*, slug=None, taskdef=TASKDEF, target=TARGET, source="spawned",
              repo="https://gitlab.example.com/agents/global.git"):
    slug = slug if slug is not None else run_state.derive_slug(taskdef, target)
    return runs.track(
        HOST, PROJECT, slug, source=source, taskdef=taskdef, target=target, repo=repo
    )


@pytest.fixture
def waiting_run(config):
    """A tracked run, present in the mirror, stopped on a recorded question."""
    plant_run(config)
    return track_run()


def request_for(slug=None, answer="yes — start empty."):
    return answer_mod.AnswerRequest(
        host=HOST,
        project=PROJECT,
        slug=slug if slug is not None else run_state.derive_slug(TASKDEF, TARGET),
        answer=answer,
    )


def child_payload(dump, timeout=10.0):
    """What the stub recorded, once it has run."""
    assert wait_for(lambda: dump.is_file() and dump.stat().st_size, timeout=timeout), (
        "the answering session never started"
    )
    return json.loads(dump.read_text(encoding="utf-8"))


def snapshot_tree(root):
    """Every file under *root* as ``{relative path: bytes}``.

    The D3 guard's instrument: the platform must not write run state, and the
    mirror is the only run state it can reach. Contents rather than mtimes, since a
    rewrite with identical bytes would still be a write the platform must not make
    — and any *new* file (a lock, a temp file, an events append) shows up as a new
    key.
    """
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- the answer reaches the child -------------------------------------------

def test_the_answer_reaches_the_child_as_one_flag_token(
    config, waiting_run, answer_recording_lmer
):
    _script, dump = answer_recording_lmer
    result = answer_mod.answer_run(config, request_for())

    payload = child_payload(dump)
    assert f"--answer={request_for().answer}" in payload["argv"], (
        "the answer must travel as a single --answer=<text> token"
    )
    assert payload["answer"] == request_for().answer, (
        "lmer's own parser must recover exactly what the operator typed"
    )
    assert payload["task"] == TASKDEF
    assert payload["target"] == [TARGET]
    assert result.session.session_id


def test_the_answer_is_never_passed_as_two_arguments(config, waiting_run):
    """``--answer -yes`` makes argparse exit 2: the value looks like an option.

    So the ``=`` spelling is not a style choice, and a regression to two tokens
    would break exactly the answers that start with a dash.
    """
    result = answer_mod.answer_run(config, request_for(answer="-yes, do it"))
    assert answer_mod.ANSWER_FLAG not in result.session.command, (
        "a bare --answer token means the text was passed as a separate argument"
    )
    assert "--answer=-yes, do it" in result.session.command


@pytest.mark.parametrize("text", [
    'he said "yes" — use $HOME/;rm -rf /`id`',
    "line one\nline two\n\n  indented three",
    "--force is what I meant",
    "-1",
    "quote'inside and a tab\there",
    "unicode: … ✓ 🙂 — naïve",
    "$(whoami) && echo pwned | tee /tmp/x",
])
def test_an_answer_survives_being_argv_not_a_shell_string(
    config, waiting_run, answer_recording_lmer, text
):
    """It is argv all the way down, so no quoting rule can eat any of this."""
    _script, dump = answer_recording_lmer
    answer_mod.answer_run(config, request_for(answer=text))

    payload = child_payload(dump)
    assert payload["answer"] == text
    assert payload["rest"] == [], (
        "no part of the answer may be mistaken for lmer's own arguments"
    )


def test_surrounding_whitespace_is_normalised_away(
    config, waiting_run, answer_recording_lmer
):
    """The container strips before applying, so this is what will be recorded."""
    _script, dump = answer_recording_lmer
    answer_mod.answer_run(config, request_for(answer="  \n yes, ship it \n "))
    assert child_payload(dump)["answer"] == "yes, ship it"


def test_the_answering_session_runs_with_a_control_plane(config, waiting_run):
    """Spec D8: whatever the platform spawns must be reachable and writable."""
    result = answer_mod.answer_run(config, request_for())
    assert "--fastapi" in result.session.command
    assert result.session.control_port


def test_the_run_is_updated_the_way_a_spawn_updates_one(config, waiting_run,
                                                        monkeypatch):
    # Keep the child alive for the registry assertion: a clean exit reaps its
    # entry, so without this the last two asserts race the watcher thread and fail
    # about one run in three. Same trap as the ports test in test_platform_spawn.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    result = answer_mod.answer_run(config, request_for())
    try:
        tracked = runs.get_tracked(HOST, PROJECT, waiting_run.slug)
        assert tracked is not None
        assert tracked.last_session_id == result.session.session_id, (
            "the detail view follows last_session_id, so it must point at the "
            "answering session rather than the one that asked"
        )
        assert tracked.source == "spawned", "answering must not re-source the run"
        entry = registry.read_session(result.session.session_id)
        assert entry is not None
        assert entry["run"] == {
            "host": HOST, "project": PROJECT, "slug": waiting_run.slug,
        }
    finally:
        os.kill(result.session.pid, 9)


def adopt_run(slug=None, *, taskdef=TASKDEF, target=TARGET):
    """Track a run the way an adoption does: no repo URL, because none is known."""
    slug = slug if slug is not None else run_state.derive_slug(taskdef, target)
    return runs.track(
        HOST, PROJECT, slug, source="adopted", taskdef=taskdef, target=target
    )


def test_an_adopted_run_with_no_recorded_repo_stays_joined_to_its_session(config):
    """The repo URL is an identity hint; without one the session orphans itself.

    An adopted run records no repo, and that is the case worth covering: the run
    identity the spawn derives has to come out the same, or the fleet view grows a
    second, host-less row for a run it is already showing.
    """
    plant_run(config)
    slug = run_state.derive_slug(TASKDEF, TARGET)
    adopt_run(slug)

    result = answer_mod.answer_run(config, request_for())

    assert result.session.host == HOST
    assert result.session.project == PROJECT
    assert result.session.slug == slug

    tracked = runs.get_tracked(HOST, PROJECT, slug)
    assert tracked.last_session_id == result.session.session_id


def test_answering_an_adopted_run_records_no_repo_url_for_it(config, monkeypatch):
    """Derived from, not written down.

    The reconstructed ``https://<host>/<project>`` is a round trip by
    construction, which is exactly what makes recording it dangerous: it passes
    every later identity check, so an adopted run answered once would carry a URL
    nobody supplied for the rest of its life — in the index, and in the entry of
    every session filed under it.
    """
    plant_run(config)
    slug = run_state.derive_slug(TASKDEF, TARGET)
    adopt_run(slug)
    # Kept alive for the registry assertion: a clean exit reaps the entry.
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")

    result = answer_mod.answer_run(config, request_for())
    try:
        assert runs.get_tracked(HOST, PROJECT, slug).repo is None, (
            "answering must not leave a fabricated repo URL in the index"
        )
        entry = registry.read_session(result.session.session_id)
        assert entry["task"]["repo"] is None
    finally:
        os.kill(result.session.pid, 9)


def test_a_run_answered_once_is_still_asked_for_its_repo_url_on_resume(config):
    """The refusal this used to undo, exercised end to end.

    ``resume`` will not invent a repo URL (``RepoUrlRequired``) because the value
    becomes the run's repo of record. A recorded reconstruction would satisfy
    that check silently — it parses back to the run's own host and project — so
    the operator would never be asked, and the guess would stick.
    """
    plant_run(config)
    slug = run_state.derive_slug(TASKDEF, TARGET)
    adopt_run(slug)

    result = answer_mod.answer_run(config, request_for())
    # The answering session must be gone before a resume: liveness is checked
    # first, and it would refuse for the wrong reason.
    assert wait_for(
        lambda: registry.read_session(result.session.session_id) is None
    ), "the answering session never exited"
    # The state the answering session would have pushed: the question is cleared,
    # so the run is resumable rather than answerable.
    plant_run(config, stop_reason=None, question=None)

    with pytest.raises(resume_mod.RepoUrlRequired, match="no repo URL recorded") as caught:
        resume_mod.resume_run(
            config, resume_mod.ResumeRequest(host=HOST, project=PROJECT, slug=slug)
        )
    assert caught.value.code == "repo_url_required"
    assert len(registry.list_sessions(live_only=False)) == 0


def test_answering_a_renamed_run_dir_still_targets_the_recorded_slug(config):
    """A renamed dir resolves by recorded slug, which is what the respawn derives.

    ``work name`` renames the directory; the state file keeps the original slug and
    that is what the container resolves on. So the directory name must not be what
    the same-run check compares against.
    """
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)
    track_run(slug=f"{recorded}--nice-name")

    result = answer_mod.answer_run(config, request_for(slug=f"{recorded}--nice-name"))

    assert result.session.slug == recorded
    assert runs.get_tracked(HOST, PROJECT, f"{recorded}--nice-name").last_session_id == (
        result.session.session_id
    ), "the key the operator answered must follow the session that was started"


# --- the platform writes no run state (spec D3) ------------------------------

def test_answering_writes_nothing_into_the_mirror(config, waiting_run):
    """The mirror is a read-only clone the daemon force-resets; a write there is
    both forbidden and futile. The *session* records the answer, in the container.
    """
    before = snapshot_tree(config.mirror_path)
    assert before, "the planted run must be in the snapshot for this to prove anything"

    answer_mod.answer_run(config, request_for())

    assert snapshot_tree(config.mirror_path) == before, (
        "the platform must not touch state.yaml, events.jsonl or anything else "
        "under the mirror (spec D3)"
    )


def test_the_question_stop_is_left_for_the_session_to_clear(config, waiting_run):
    state_file = (
        config.mirror_path / HOST / PROJECT / "runs" / waiting_run.slug / "state.yaml"
    )
    answer_mod.answer_run(config, request_for())
    text = state_file.read_text(encoding="utf-8")
    assert "stop_reason: question" in text
    assert QUESTION in text


# --- the answer is content, not something to copy around ---------------------

def test_the_answer_text_never_reaches_the_platform_event_log(config, waiting_run):
    secret_ish = "the password is hunter2, use it for the staging db"
    answer_mod.answer_run(config, request_for(answer=secret_ish))

    events = store.read_events()
    assert any(event["type"] == "run_answered" for event in events)
    assert secret_ish not in json.dumps(events)
    answered = [e for e in events if e["type"] == "run_answered"][-1]
    assert answered["data"]["answer_chars"] == len(secret_ish)
    assert answered["data"]["run"]["slug"] == waiting_run.slug


def test_the_answer_text_is_not_echoed_back_to_the_caller(config, waiting_run):
    """``SpawnResult.to_dict`` publishes the argv, and the argv carries the answer."""
    text = "answer body that must not come back"
    result = answer_mod.answer_run(config, request_for(answer=text))

    payload = result.to_dict()
    assert text not in json.dumps(payload)
    assert "command" not in json.dumps(payload), (
        "publishing the spawn's command would publish --answer=<text> with it"
    )
    assert payload["question"] == QUESTION
    assert payload["session"]["session_id"] == result.session.session_id


def test_the_answer_text_is_not_logged(config, waiting_run, caplog):
    caplog.set_level("INFO")
    text = "do not log me anywhere"
    answer_mod.answer_run(config, request_for(answer=text))
    assert any("platform_run_answered" in r.message for r in caplog.records)
    assert all(text not in r.getMessage() for r in caplog.records)


# --- refusals ---------------------------------------------------------------

def test_an_untracked_run_is_refused(config):
    """Scope is the local index (D25): a colleague's run is not ours to restart."""
    plant_run(config)
    with pytest.raises(answer_mod.RunNotTracked, match="not tracked") as caught:
        answer_mod.answer_run(config, request_for())
    assert caught.value.status == 404
    assert registry.list_sessions(live_only=False) == []


def test_a_run_missing_from_the_mirror_is_refused(config):
    track_run()
    with pytest.raises(answer_mod.NotAnswerable, match="not in the host mirror"):
        answer_mod.answer_run(config, request_for())
    assert registry.list_sessions(live_only=False) == []


def test_a_refusal_names_the_run_key_not_a_directory_it_guessed(config):
    """Refusals used to compose ``runs/<slug>`` out of the request, and a named run
    has no such directory — it is at ``runs/<slug>--<name>`` (T90). So the message
    that told an operator what to fix pointed them at a path nobody can open. The
    key is what the run is tracked under and what a corrected request repeats."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)

    with pytest.raises(answer_mod.RunNotTracked) as caught:
        answer_mod.answer_run(config, request_for(slug=recorded))

    message = str(caught.value)
    assert message.startswith(f"{HOST}/{PROJECT}/{recorded} is not tracked")
    assert f"runs/{recorded}" not in message, "no directory is invented here"


def test_the_live_session_refusal_names_the_run_key_too(config):
    """The other message built from the request, and the one whose slug is not even
    the request's: it is the identity the live session filed itself under."""
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)
    track_run(slug=recorded)
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": recorded},
    )

    with pytest.raises(answer_mod.NotAnswerable) as caught:
        answer_mod.answer_run(config, request_for(slug=recorded))

    message = str(caught.value)
    assert message.startswith(f"{HOST}/{PROJECT}/{recorded} already has a live session")
    assert f"runs/{recorded}" not in message


def test_a_run_whose_state_vanishes_mid_answer_is_refused(config, waiting_run, monkeypatch):
    """The mirror is force-reset under the answer path, not frozen for it.

    Every fleet poll calls ``pull()``, which ``git reset --hard``s the mirror — so
    the state file really can disappear between resolving the run dir and reading
    it. ``load_state`` answers ``None`` for that, and the difference between a
    refusal and an ``AttributeError`` 500 is this check.
    """
    resolve = answer_mod.resolve_run_dir

    def resolve_then_reset(*args, **kwargs):
        ref = resolve(*args, **kwargs)
        (ref.path / "state.yaml").unlink()
        return ref

    monkeypatch.setattr(answer_mod, "resolve_run_dir", resolve_then_reset)

    with pytest.raises(answer_mod.NotAnswerable, match="no readable run state"):
        answer_mod.answer_run(config, request_for())
    assert registry.list_sessions(live_only=False) == []


def test_an_identity_with_stray_whitespace_still_resolves(config, waiting_run):
    """The index strips and a filesystem path does not, so the two would disagree.

    A copy-pasted or shell-quoted identity is the ordinary way to get here, and the
    failure it would otherwise produce ("not in the host mirror") sends debugging
    at the mirror instead of at the whitespace.
    """
    result = answer_mod.answer_run(
        config,
        answer_mod.AnswerRequest(
            host=f" {HOST} ", project=f"{PROJECT}\t", slug=f" {waiting_run.slug}",
            answer="yes",
        ),
    )
    assert result.session.slug == waiting_run.slug


def test_a_run_with_unreadable_state_is_refused(config):
    plant_run(config)
    track_run()
    path = config.mirror_path / HOST / PROJECT / "runs" / request_for().slug
    (path / "state.yaml").write_text("not: [a, mapping\n", encoding="utf-8")

    with pytest.raises(answer_mod.NotAnswerable, match="could not be read"):
        answer_mod.answer_run(config, request_for())


@pytest.mark.parametrize("status", ["complete", "archived"])
def test_a_finished_run_is_refused(config, status):
    """The fleet view calls such a run complete and offers no answer; nor does this.

    ``answer_question`` leaves ``status`` alone by design, so the respawned session
    would arrive holding an answer *and* the completed-run directive telling it not
    to act — a container started to do nothing.
    """
    plant_run(config, status=status)
    track_run()

    with pytest.raises(answer_mod.NotAnswerable, match=f"is {status}"):
        answer_mod.answer_run(config, request_for())
    assert registry.list_sessions(live_only=False) == []


def test_a_failed_index_write_does_not_report_a_failed_answer(
    config, waiting_run, monkeypatch, caplog
):
    """The container already has the answer by then, so this must not raise.

    Reporting it as failed would send the operator to answer again, which the
    live-session check then refuses — leaving them with two contradictory errors
    for an answer that was in fact delivered.
    """
    def unwritable(*_args, **_kwargs):
        raise store.StoreError("cannot write runs.json (disk full)")

    monkeypatch.setattr(runs, "note_session", unwritable)
    result = answer_mod.answer_run(config, request_for())

    assert result.session.session_id
    assert any(
        "platform_answer_index_not_updated" in r.message for r in caplog.records
    )


@pytest.mark.parametrize("stop_reason", [None, "paused", "yield", "critical_error"])
def test_a_run_not_stopped_on_a_question_is_refused(config, stop_reason):
    plant_run(config, stop_reason=stop_reason, question=None)
    track_run()

    with pytest.raises(answer_mod.NotAnswerable, match="not stopped on a question") as caught:
        answer_mod.answer_run(config, request_for())
    assert caught.value.status == 409
    assert registry.list_sessions(live_only=False) == []


@pytest.mark.parametrize("question", [None, "", "   "])
def test_a_question_stop_with_no_recorded_text_is_answered(
    config, question, answer_recording_lmer
):
    """Refused until T24, and it is the shape most runs stop in.

    ``work session-start`` keys on the question stop rather than on the recorded
    text now, so the respawn lands: the session records the answer against a null
    question and clears the stop. The reply's ``question`` is that null — a client
    can tell "nothing was written down" from a question it can echo, which an empty
    string would not.
    """
    _script, dump = answer_recording_lmer
    plant_run(config, question=question)
    track_run()

    result = answer_mod.answer_run(config, request_for())

    assert result.question is None
    assert result.to_dict()["question"] is None
    assert child_payload(dump)["answer"] == request_for().answer, (
        "the answer has to reach the child, or this is a slot spent on nothing"
    )


def test_the_session_side_applies_the_stop_this_module_now_allows(tmp_path):
    """The allow above is only right because the container applies it.

    Calling what ``work session-start`` calls, on the state shape it would find: a
    question stop with no text. If ``answer_question`` ever went back to requiring
    the text, the failure belongs here rather than in a container an operator is
    waiting on — the platform cannot see that it happened.
    """
    rdir = tmp_path / "runs" / "develop-issue-141"
    state = {"schema": 1, "slug": "develop-issue-141", "status": "in-progress",
             "stop_reason": "question", "open_question": None}
    run_state.write_state(rdir, state)

    updated = run_state.answer_question(rdir, state, "yes — start empty.")

    assert updated["stop_reason"] is None
    event = run_state.read_events(rdir, last_n=0)[-1]
    assert event["type"] == "question_answered"
    assert event["data"] == {"question": None, "answer": "yes — start empty."}


def test_a_caller_cannot_smuggle_an_answer_past_these_refusals(config, waiting_run):
    """``POST /api/sessions`` takes ``extra_args``, and every check is in here.

    So the flag is reserved there (``spawn._RESERVED_ARGS``) and this module uses
    the typed field instead. Without that, a raw spawn *is* an answer — with no
    question check, no same-run check and no live-session check in front of it.
    """
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(config, spawn.SpawnRequest(
            taskdef=TASKDEF, target=TARGET,
            extra_args=(f"{answer_mod.ANSWER_FLAG}=smuggled",),
        ))
    assert registry.list_sessions(live_only=False) == []


def test_a_run_with_a_live_session_is_refused(config, waiting_run):
    """Liveness outranks committed state (D24), and two containers for one run
    would fight over its owner claim."""
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": waiting_run.slug},
    )
    with pytest.raises(answer_mod.NotAnswerable, match="already has a live session") as caught:
        answer_mod.answer_run(config, request_for())
    assert "s-live" in str(caught.value)
    assert len(registry.list_sessions(live_only=False)) == 1


def test_a_live_session_under_a_renamed_runs_recorded_slug_is_seen(config):
    """The identity a session registers under is the recorded slug, not the dir name.

    So for a renamed run the tracked key and the session key differ, and a check
    that only knew the tracked key would happily start a second container.
    """
    recorded = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=f"{recorded}--nice-name", recorded_slug=recorded)
    track_run(slug=f"{recorded}--nice-name")
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": recorded},
    )

    with pytest.raises(answer_mod.NotAnswerable, match="already has a live session"):
        answer_mod.answer_run(config, request_for(slug=f"{recorded}--nice-name"))
    assert len(registry.list_sessions(live_only=False)) == 1


def test_a_dead_session_does_not_block_an_answer(config, waiting_run):
    """A stale entry is a crash signal, not a duplicate-spawn risk."""
    registry.register(
        "s-dead", pid=2**22,
        run={"host": HOST, "project": PROJECT, "slug": waiting_run.slug},
    )
    result = answer_mod.answer_run(config, request_for())
    assert result.session.session_id


def test_answering_twice_is_refused_by_the_live_session_check(
    config, waiting_run, monkeypatch
):
    """Which is also what makes a double-tapped answer button harmless.

    The first session is kept alive deliberately: it is the live entry the second
    attempt has to trip over, and a stub that exits immediately would let the second
    answer through for reasons that have nothing to do with the guard.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    first = answer_mod.answer_run(config, request_for())
    try:
        assert wait_for(
            lambda: registry.is_live(registry.read_session(first.session.session_id))
        )
        with pytest.raises(answer_mod.NotAnswerable, match="already has a live session"):
            answer_mod.answer_run(config, request_for())
        assert len(registry.list_sessions(live_only=True)) == 1
    finally:
        os.kill(first.session.pid, 9)


def test_the_spawn_still_refuses_a_duplicate_with_this_check_defeated(
    config, waiting_run, monkeypatch
):
    """Where the rule actually lives, and why this module's copy is not the fix.

    The incident behind it: a run acquired a second session while the first was
    still up, because the checks that stood between an answer and a spawn were
    *route-local* — one here, one in ``resume``, none on ``POST /api/sessions``. The
    invariant is in ``spawn_session`` now, on the identity the spawn is about to
    register, so removing the guard above must not get a duplicate through.

    Monkeypatched rather than reasoned about: this is the one way a test can stand
    where the next caller stands — the one that reaches ``spawn_session`` with no
    check of its own.
    """
    monkeypatch.setattr(answer_mod, "_refuse_if_live", lambda *_a, **_kw: None)
    registry.register(
        "s-live", pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": waiting_run.slug},
    )

    with pytest.raises(spawn.RunAlreadyLive, match="already has a live session"):
        answer_mod.answer_run(config, request_for())
    assert len(registry.list_sessions(live_only=False)) == 1, (
        "the answer spawned a second session for a run that already had one"
    )


@pytest.mark.parametrize("bad", ["", "   ", "\n\t ", None, 5, ["a"]])
def test_an_empty_or_non_string_answer_is_refused(config, waiting_run, bad):
    """``lmer`` maps an empty ``--answer`` to no ``LMER_ANSWER`` at all, and the
    container strips before testing for content — so blank never reaches the run."""
    with pytest.raises(answer_mod.AnswerError) as caught:
        answer_mod.answer_run(config, request_for(answer=bad))
    assert caught.value.status == 400
    assert registry.list_sessions(live_only=False) == []


def test_an_oversized_answer_is_refused_before_the_spawn_fails(config, waiting_run):
    """Otherwise ``execve`` answers with an opaque "Argument list too long"."""
    with pytest.raises(answer_mod.AnswerError, match="over the"):
        answer_mod.answer_run(
            config, request_for(answer="x" * (answer_mod.MAX_ANSWER_CHARS + 1))
        )
    assert registry.list_sessions(live_only=False) == []


def test_an_answer_at_the_limit_is_accepted(config, waiting_run):
    result = answer_mod.answer_run(
        config, request_for(answer="x" * answer_mod.MAX_ANSWER_CHARS)
    )
    assert result.session.session_id


def test_the_limit_stays_under_the_kernel_argument_ceiling():
    """MAX_ARG_STRLEN is 128 KiB; four bytes per character is the UTF-8 worst case."""
    assert answer_mod.MAX_ANSWER_CHARS * 4 < 131072


@pytest.mark.parametrize("missing", [
    {"taskdef": None},
    {"target": None},
    {"taskdef": None, "target": None},
])
def test_a_run_the_platform_cannot_respawn_is_refused(config, missing):
    """The index is the fallback for state that records neither — and an adopted
    run has nothing in the index either, so there is genuinely nothing to spawn."""
    plant_run(config, slug="develop-orphan", recorded_slug="develop-orphan", **missing)
    runs.track(HOST, PROJECT, "develop-orphan", source="adopted")

    with pytest.raises(answer_mod.NotAnswerable, match="cannot work out what to respawn"):
        answer_mod.answer_run(config, request_for(slug="develop-orphan"))
    assert registry.list_sessions(live_only=False) == []


def test_the_index_fills_in_a_taskdef_the_state_file_lacks(config):
    """Old state files predate these fields; the index is what remembers them."""
    slug = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=slug, taskdef=None, target=None)
    track_run(slug=slug)

    result = answer_mod.answer_run(config, request_for(slug=slug))
    assert result.session.command[1:3] == [TASKDEF, TARGET]


def test_a_respawn_that_would_land_on_another_run_is_refused(config):
    """The container derives its own run dir, so a mismatched pair answers nothing.

    Here the index and the state file disagree with the run's recorded slug — which
    is what a hand-edited or mis-adopted entry looks like. Spawning would seed a
    different run and apply the answer to a question it never asked.
    """
    plant_run(
        config, slug="develop-issue-999", recorded_slug="develop-issue-999",
        taskdef=TASKDEF, target=TARGET,
    )
    runs.track(HOST, PROJECT, "develop-issue-999", source="adopted",
               taskdef=TASKDEF, target=TARGET)

    with pytest.raises(answer_mod.NotAnswerable, match="would land on a different run"):
        answer_mod.answer_run(config, request_for(slug="develop-issue-999"))
    assert registry.list_sessions(live_only=False) == []


def test_answer_follows_a_run_that_reslugged_from_the_tracked_key(config):
    base = run_state.derive_slug(TASKDEF, TARGET)
    current = f"{base}-v1.2.3"
    plant_run(
        config,
        slug=current,
        recorded_slug=current,
        extra=f"reslugged_from:\n  - {base}",
    )
    track_run(slug=base)

    result = answer_mod.answer_run(config, request_for(slug=base))

    assert result.session.slug == base


def test_a_state_file_with_no_slug_is_matched_on_its_directory_name(config):
    """Nothing but the directory name can resolve such a run, so that is the check."""
    slug = run_state.derive_slug(TASKDEF, TARGET)
    plant_run(config, slug=slug, recorded_slug=None)
    track_run(slug=slug)

    assert answer_mod.answer_run(config, request_for(slug=slug)).session.slug == slug


@pytest.mark.parametrize("identity", [
    {"host": ""}, {"project": "  "}, {"slug": ""},
    {"host": 5}, {"slug": None},
])
def test_an_incomplete_identity_is_refused(config, identity):
    payload = {"host": HOST, "project": PROJECT, "slug": "develop-1", "answer": "hi"}
    payload.update(identity)
    with pytest.raises(answer_mod.AnswerError) as caught:
        answer_mod.answer_run(config, answer_mod.AnswerRequest(**payload))
    assert caught.value.status == 400


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
    """These become path segments under the mirror, and this verb starts something.

    Reachable only after such an identity was tracked, which takes the shared
    secret — but "authenticated" is not "allowed to name a directory outside the
    mirror as a run".
    """
    payload = {"host": HOST, "project": PROJECT, "slug": "develop-1", "answer": "hi"}
    payload.update(identity)
    request = answer_mod.AnswerRequest(**payload)
    runs.track(request.host, request.project, request.slug, source="adopted",
               taskdef=TASKDEF, target=TARGET)

    with pytest.raises(answer_mod.AnswerError, match="path"):
        answer_mod.answer_run(config, request)
    assert registry.list_sessions(live_only=False) == []


def test_the_capacity_cap_still_applies_to_an_answer(platform_root, fake_lmer):
    """An answer is not a reason to exceed the cap — it is a spawn like any other."""
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    plant_run(config)
    track_run()
    registry.register("s-occupant", pid=os.getpid())

    with pytest.raises(spawn.CapacityError, match="1/1"):
        answer_mod.answer_run(config, request_for())


# --- the seam between the refusal and what the UI says -----------------------

def test_the_uis_no_question_copy_matches_the_note_the_backend_emits():
    """AnswerBox disables the field on that note, so a drift would be a trap.

    The fleet payload carries no structural "was a question recorded" flag — the
    attention note is either the question or the sentence saying there is none — so
    the UI compares against that sentence. This pins the pair: the backend's
    sentence comes from actually deriving a text-less question stop, not from a
    copy of the literal.
    """
    _state, attention = inventory._derive(
        {"status": "in-progress", "stop_reason": "question"}, None
    )
    source = (WEB / "src" / "components" / "AnswerBox.vue").read_text(encoding="utf-8")
    match = re.search(r"const NO_QUESTION_NOTE = '([^']+)'", source)
    assert match, "AnswerBox no longer names the note it compares against"
    assert match.group(1) == attention.note, (
        "AnswerBox's NO_QUESTION_NOTE must be the note inventory emits for a "
        "question stop with no recorded text"
    )


def test_the_answer_box_states_that_answering_starts_a_session():
    """It looks like a chat composer and is not one; the copy is the only warning."""
    source = (WEB / "src" / "components" / "AnswerBox.vue").read_text(encoding="utf-8")
    assert "starts a new session" in source
    assert "has exited" in source


def test_the_answer_box_does_not_gate_the_field_on_a_recorded_question():
    """The missing text is context now, not a refusal — the backend allows it.

    Source-level because there is no JS runner here, and specific because the two
    ways it used to block are the two that would silently come back: the send
    predicate and the `v-if` around the field. A UI that refuses what the daemon
    accepts is the same trap as the reverse, pointed the other way.
    """
    source = (WEB / "src" / "components" / "AnswerBox.vue").read_text(encoding="utf-8")
    send = re.search(r"const canSend = computed\(\s*\(\) =>([^)]+)\)", source)
    assert send, "AnswerBox no longer has a canSend predicate"
    assert "questionRecorded" not in send.group(1), (
        "a question stop with no recorded text is answerable; canSend must not "
        "require the text"
    )
    assert '<template v-if="questionRecorded">' not in source, (
        "the answer field must be rendered for a text-less question stop too"
    )
    # The copy stays: it says why there is nothing to read, not that the run
    # cannot be answered.
    assert "did not save the question's text" in source
    assert "Your answer still reaches the run" in source


def test_the_chat_view_sends_a_blocked_run_to_the_answer_box():
    """Its composer is deliberately absent, so its alert is the only signpost.

    It pointed at the terminal while the answer flow was unbuilt; pointing there
    now would send an operator to drive a container that has already exited.
    """
    source = (WEB / "src" / "components" / "Chat.vue").read_text(encoding="utf-8")
    assert "answer box above" in source
    assert "respawned with" in source
    assert "terminal below" not in source


def test_the_run_card_offers_a_way_through_to_answering():
    """The row says answering is possible, and which kind of answering it is.

    T23 added a second question case — a *live* session waiting on its ask
    channel — so the button's label became a computed one. It must still say
    "answer" for a stopped run: that word is the promise that a container starts.
    """
    source = (WEB / "src" / "components" / "RunCard.vue").read_text(encoding="utf-8")
    assert "answerable" in source
    assert ">{{ answerLabel }}</v-btn>" in source
    assert "'reply' : 'answer'" in source
