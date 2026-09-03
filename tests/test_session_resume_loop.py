"""A resume lands in the record it resumed — host flag to state file (issue #353).

In schema form the SESSION is the unit of record (D22): every launch owns
`<project>/_runs/<slug>/<session-id>[--<name>]/`, and `_runs/<slug>/` is a
grouping level nothing is written at. That makes "which record does this
launch write in" one question with one answer — `LMER_SESSION_ID` — and the
resume loop is closed only if the SAME id travels the whole way:

1. the host CLI puts the resolved record's id in the container environment
   (`lmer --session <id>` -> `LMER_SESSION_ID`, the leg
   `tests/test_lmer_cli_args.py` pins at the flag);
2. container start reads it and prepares that record's address without
   opening a second one beside it (`clone_and_exec.ensure_work_repo_directory`,
   the leg `tests/test_clone_and_exec_session_dir.py` pins at the entrypoint);
3. `work session-start` — invoked by `hooks/start.py` with NO `--session`,
   because the environment already says which launch this is — claims the
   record that was already there.

Each leg has its own tests and each of them can pass while the loop stays
open: a flag nothing injects, an entrypoint that pre-creates a dir the work
CLI then ignores, a work CLI that would route correctly if only it were told.
This file is the join. It drives the legs in order, with the id read back out
of the container environment the host CLI actually built rather than
hard-coded, and asserts the three outcomes that matter: a resume writes in
the record the operator named and creates nothing beside it; an agent that
spawns a child session from inside a container does not hand the child its
own record; and a launch with a freshly minted id still opens its own record
— the fix must not route a resume by disabling a new run.

The container-side legs run the real functions in-process (the entrypoint's
`ensure_work_repo_directory`, the work CLI's `cmd_session_start`); the host
leg runs the real `main()` with the container runtime mocked out, reusing the
harness `tests/test_lmer_env_file.py` uses, and hands its captured env dict to
the real `build_container_env` — so what is asserted is what the runtime
would be given, after the None filtering, not what the dict said before it.
"""

import os
from unittest.mock import patch

import pytest

from lmer_cli.container import clone_and_exec
from lmer_cli.runtime import build_container_env
from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    PRIMARY_REPO,
    SESSION_DIR_NAMES,
    SESSION_IDS,
    SESSION_NAME,
    SLUG,
    TARGET,
    TASKDEF,
    build_schema_plexus,
)
from tests.test_container_env_transport import _parsed_env_file
from tests.test_lmer_cli_slack_target import _BASE_ENV, _make_main_mocks
from work_repo import cli as work_cli
from work_repo import run_state, utils

#: The record this run's operator asked to continue: the fixture tree's
#: NAMED launch, whose directory is `<sid>--second-look` while the id an
#: operator (and the platform) quotes is the bare `<sid>` half.
RESUMED_ID = SESSION_IDS[1]
RESUMED_DIR_NAME = SESSION_DIR_NAMES[1]

PRIMARY_HOST, PRIMARY_PATH = PRIMARY_REPO.split("/", 1)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, no announcement and no synthesized id carried
    between tests — plus `LMER_SESSION_ID` registered with monkeypatch even
    when the host has none, because `mint_session_id` writes straight into
    `os.environ` and a minted id must not outlive its test."""
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("LMER_SESSION_ID", "")
    monkeypatch.delenv("LMER_SESSION_ID")
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    run_state._reset_synthesized_session_id()


# ---------------------------------------------------------------------------
# Leg 1: what the host CLI actually delivers to the container
# ---------------------------------------------------------------------------


def _delivered_container_env(*flags, host_env=None):
    """Every variable an `lmer <flags>` invocation delivers to the container.

    The real `main()` under the standard mock stack, with the env dict it
    builds handed to the real `build_container_env`: the answer therefore
    includes the None filtering (a variable the CLI set to None is delivered
    as ABSENT, which is not the same as delivered empty) and both transport
    legs — the `--env-file` line a session id takes, and the inherited
    `-e NAME` marker a value with a newline in it would take. Neither ever
    puts the value in argv (`tests/test_container_env_transport.py`), so a
    session id is read back from the delivery, never from the command line.

    `--no-task --exec true`: the leg under test is the environment, and a
    taskdef lookup would drag the taskdef sources into a test about one
    variable. The container-side legs below get their run context from
    `_set_context` instead, exactly as a container gets it from the host's
    other exports.
    """
    captured: dict = {}
    with patch.dict(os.environ, {**_BASE_ENV, **(host_env or {})}, clear=True):
        with _make_main_mocks(captured_env=captured):
            from lmer_cli.cli import main

            main(["--no-task", "--exec", "true", f"https://{PRIMARY_REPO}", *flags])
    container_env = build_container_env(captured)
    try:
        delivered = _parsed_env_file(container_env)
        delivered.update(container_env.client_env)
        return delivered
    finally:
        container_env.cleanup()


# ---------------------------------------------------------------------------
# Legs 2 and 3: what the container does with it
# ---------------------------------------------------------------------------


def _set_context(monkeypatch, root, session):
    """The launch context a container carries: the work repo, the run's
    (taskdef, target), and this launch's id."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv("LMER_REPO_HOST", PRIMARY_HOST)
    monkeypatch.setenv("LMER_REPO_PROJECT", PRIMARY_PATH)
    monkeypatch.setenv("LMER_TASK", TASKDEF)
    monkeypatch.setenv("LMER_TASK_TARGET", TARGET)
    monkeypatch.setenv("LMER_SESSION_ID", session)


def _container_start(root):
    """Container start's work-repo step, the real one (`clone_and_exec.main`
    calls it with exactly these arguments, from exactly these variables)."""
    clone_and_exec.ensure_work_repo_directory(
        root, PRIMARY_HOST, PRIMARY_PATH, TASKDEF, TARGET
    )


def _session_start():
    """`work session-start` with no `--session`, the way `hooks/start.py`
    invokes it: the environment already says which launch this is."""
    with patch.object(work_cli.sys, "argv", ["work", "session-start"]):
        return work_cli.main()


def _dirs_in(group):
    return sorted(entry.name for entry in group.iterdir() if entry.is_dir())


@pytest.fixture
def schema_tree(tmp_path):
    return build_schema_plexus(tmp_path / "plexus-schema")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class TestAResumeLandsInTheRecordItResumed:
    """The whole loop, in the order a resumed container runs it."""

    @pytest.fixture
    def resumed(self, monkeypatch, schema_tree):
        """A launch that started the way the platform starts a resume: the
        id comes out of the container environment the host CLI built for
        `lmer --session <the record's id>`, and nothing else about the
        launch says which record it belongs to."""
        injected = _delivered_container_env("--session", RESUMED_ID)
        assert injected.get("LMER_SESSION_ID") == RESUMED_ID, (
            "the host CLI delivered no session id, so nothing below is a "
            f"resume: delivered {injected.get('LMER_SESSION_ID')!r}"
        )
        _set_context(monkeypatch, schema_tree.root, injected["LMER_SESSION_ID"])
        _container_start(schema_tree.root)
        assert _session_start() == 0
        return schema_tree

    def test_the_group_gained_no_new_child(self, resumed):
        """Not "no new record" — no new CHILD. An empty `<sid>/` beside the
        `<sid>--<name>/` it belongs to routes nothing wrong (a child with no
        state file is not a record to any resolver) but it is litter at the
        one level D22 keeps clean: the validator warns on it and an operator
        reading the group sees two directories for one launch."""
        assert _dirs_in(resumed.runs_dir / SLUG) == sorted(SESSION_DIR_NAMES)

    def test_the_launch_writes_in_the_record_that_was_there(self, resumed):
        """The record is continued, not reseeded: the fields the fixture
        wrote are still the ones on disk, and the launch's own start event
        was appended to the history that was already in it."""
        record = resumed.runs_dir / SLUG / RESUMED_DIR_NAME
        state = run_state.load_state(record)
        assert state["name"] == SESSION_NAME
        assert str(state["created"]).startswith("2026-08-24T11:30:00")
        events = run_state.read_events(record, last_n=0)
        assert [event["type"] for event in events[:2]] == [
            "run_seeded", "session_start"
        ]
        assert events[-1]["type"] == "session_start"
        assert events[-1]["session"] == RESUMED_ID

    def test_run_dir_resolves_to_that_record(self, resumed):
        """The rest of the launch is a stream of fresh processes that know
        only their own `LMER_SESSION_ID`; every one of them must resolve to
        the same directory the session start claimed."""
        assert run_state.run_dir() == resumed.runs_dir / SLUG / RESUMED_DIR_NAME

    def test_the_resumed_record_is_the_only_one_the_fleet_lists(self, resumed):
        """The failure this closes is silent — a normal-looking brief for a
        brand-new record — so what the session listing shows afterwards is
        the assertion an operator would have noticed."""
        assert [row["id"] for row in run_state.list_sessions()] == list(SESSION_IDS)


class TestAChildSessionIsNotRoutedIntoItsParentsRecord:
    """The reason the host leg is flag-only, tested as behaviour.

    `LMER_SESSION_ID` is exported inside every lmer container
    (libexec/harness-common.sh), so an agent that runs `lmer` to spawn a
    child session has its OWN record's id in the environment it spawns from.
    Reading it as a default would hand the child its parent's record: two
    containers writing one state file, which is the collision the
    session-level record exists to prevent.
    """

    def test_an_exported_id_is_not_delivered(self):
        parent = {"LMER_SESSION_ID": SESSION_IDS[0]}
        assert "LMER_SESSION_ID" not in _delivered_container_env(host_env=parent)

    def test_the_flag_is_still_delivered_from_such_a_parent(self):
        """The other half: a spawner that DOES name a record still gets it —
        the refusal is of the ambient value, not of the feature."""
        parent = {"LMER_SESSION_ID": SESSION_IDS[0]}
        delivered = _delivered_container_env(
            "--session", RESUMED_ID, host_env=parent
        )
        assert delivered["LMER_SESSION_ID"] == RESUMED_ID


class TestAFreshLaunchStillOpensItsOwnRecord:
    """The converse, in the same file on purpose: routing a resume must not
    be bought by disabling a new run."""

    @pytest.fixture
    def launched(self, monkeypatch, schema_tree):
        """A launch the host injected nothing into: `mint_session_id` mints
        the id (its `setdefault` is what preserves an injected one), and the
        same two container steps follow."""
        assert "LMER_SESSION_ID" not in _delivered_container_env()
        clone_and_exec.mint_session_id()
        _set_context(monkeypatch, schema_tree.root, os.environ["LMER_SESSION_ID"])
        _container_start(schema_tree.root)
        assert _session_start() == 0
        return schema_tree, os.environ["LMER_SESSION_ID"]

    def test_the_group_gained_the_new_launchs_record(self, launched):
        tree, minted = launched
        assert _dirs_in(tree.runs_dir / SLUG) == sorted([*SESSION_DIR_NAMES, minted])

    def test_the_new_record_is_a_record(self, launched):
        """A directory is not a record until it holds state — the empty
        address container start prepares is exactly what this asserts is no
        longer empty."""
        tree, minted = launched
        record = tree.runs_dir / SLUG / minted
        assert run_state._state_path(record) is not None
        assert run_state.load_state(record)["slug"] == SLUG
        assert run_state.run_dir() == record

    def test_neither_existing_record_was_touched(self, launched):
        """A new launch adopts nothing: both fixture records keep the status
        they had."""
        tree, _ = launched
        first, second = tree.session_dirs
        assert run_state.load_state(first)["status"] == "complete"
        assert run_state.load_state(second)["status"] == "in-progress"


class TestTheAnswerRerouteYieldsToALaunchThatHasARecord:
    """Two routes to one record must not disagree — and one of them must yield.

    Before the host injected an id, a relaunch carrying `LMER_ANSWER` had no
    way to reach the record that asked: it was a new container with a new
    `LMER_SESSION_ID`, so the kernel opened a fresh session dir beside the
    stopped one and the question stood forever. `work_repo.cli.
    _session_that_asked` is the fallback written for exactly that, and it
    reroutes such a launch to the newest asking record of its run.

    The injected id is now the direct route, which leaves two mechanisms aimed
    at the same launch. The rule they yield by is the reroute's first test:
    whether this launch already HAS a record
    (`run_state._state_path(own) is not None`).

    That condition is "the id names a record that EXISTS", not "an id was
    injected", and the class is named for the first because only the first is
    always true (!268 review). Every id the platform injects was read off disk,
    so for the whole resume and answer path the two can never pick different
    records — only one of them ever answers. A HAND-TYPED id naming a record
    that does not exist yet is the case where both are aimed, and the last test
    here pins what actually happens rather than leaving the reader to infer it
    from a claim that would be too strong.
    """

    @pytest.fixture(autouse=True)
    def _own_marker_dir(self, monkeypatch, tmp_path):
        """The pushed answer's consume-once marker, per test.

        `_answer_marker_path` is keyed on the ANSWER TEXT under /tmp, so two
        tests answering the same question share one marker: the first
        consumes it and the rest read their answer as already applied. That
        is ambient state deciding a test's outcome — the contamination class
        !258 found — and `ANSWER_MARKER_DIR` exists for exactly this.
        """
        monkeypatch.setattr(work_cli, "ANSWER_MARKER_DIR", str(tmp_path / "markers"))

    @pytest.fixture
    def asking_record(self, schema_tree):
        """The named record, stopped on a question — the shape a relaunch
        carrying an answer arrives for."""
        record = schema_tree.runs_dir / SLUG / RESUMED_DIR_NAME
        state = run_state.load_state(record)
        state["stop_reason"] = "question"
        state["open_question"] = "Should the queue survive a restart?"
        run_state.write_state(record, state)
        return schema_tree, record

    def test_the_reroute_declines_because_the_launch_has_a_record(
        self, monkeypatch, asking_record
    ):
        tree, record = asking_record
        injected = _delivered_container_env("--session", RESUMED_ID)
        _set_context(monkeypatch, tree.root, injected["LMER_SESSION_ID"])
        monkeypatch.setenv("LMER_ANSWER", "Yes — persist it.")
        _container_start(tree.root)

        assert work_cli._session_that_asked() is None, (
            "the reroute must stand down once the launch has a record of its "
            "own; two mechanisms both routing is how they come to disagree"
        )
        assert run_state.run_dir() == record

    def test_the_reroute_is_live_when_nothing_was_injected(
        self, monkeypatch, asking_record
    ):
        """The converse, so the test above is not vacuous.

        A reroute that never fires would make "it stands down" true for the
        wrong reason. With a FRESH id — a relaunch that carries the answer and
        nothing else, the shape the fallback was written for — it does fire
        and names the record that asked. So the assertion above is measuring
        the injected id, not a dead mechanism.
        """
        tree, record = asking_record
        clone_and_exec.mint_session_id()
        _set_context(monkeypatch, tree.root, os.environ["LMER_SESSION_ID"])
        monkeypatch.setenv("LMER_ANSWER", "Yes — persist it.")
        _container_start(tree.root)

        assert work_cli._session_that_asked() == RESUMED_ID

    def test_a_hand_typed_id_naming_no_record_loses_to_the_reroute(
        self, monkeypatch, asking_record
    ):
        """The case the class docstring used to over-claim away.

        `lmer --session <an id nothing occupies> --answer "…"` aims both
        mechanisms at once: the launch has no record, so the reroute does not
        stand down, and it wins. The outcome is the useful one — the answer is
        applied where the question is, not filed in an empty record beside it —
        so this pins the behaviour rather than asking for a different one. What
        it stops is a reader trusting "an injected id always decides", which is
        true of every id the PLATFORM injects and not of one typed by hand.

        The address the id named is left behind empty. That is the entrypoint's
        stray session dir on the non-injected route, which is its own item in
        the remaining fix set; it is asserted here so the cost is recorded
        where the behaviour is, rather than discovered again later.
        """
        tree, record = asking_record
        unoccupied = "20260901-120000-9-99999"
        injected = _delivered_container_env("--session", unoccupied)
        assert injected["LMER_SESSION_ID"] == unoccupied
        _set_context(monkeypatch, tree.root, unoccupied)
        monkeypatch.setenv("LMER_ANSWER", "Yes — persist it.")
        _container_start(tree.root)

        assert work_cli._session_that_asked() == RESUMED_ID
        assert _session_start() == 0

        # The answer landed where the question was, not in the named address.
        assert run_state.load_state(record)["stop_reason"] is None
        assert not (tree.runs_dir / SLUG / unoccupied / "state.yaml").exists()
        assert (tree.runs_dir / SLUG / unoccupied).is_dir(), (
            "the named address is created and then unused — the stray session "
            "dir on the non-injected route, recorded here rather than implied"
        )

    def test_the_answer_lands_in_that_record_and_opens_nothing_beside_it(
        self, monkeypatch, asking_record
    ):
        """The outcome the operator sees: the question they answered is the
        question that got cleared, in the record they answered it for."""
        tree, record = asking_record
        injected = _delivered_container_env("--session", RESUMED_ID)
        _set_context(monkeypatch, tree.root, injected["LMER_SESSION_ID"])
        monkeypatch.setenv("LMER_ANSWER", "Yes — persist it.")
        _container_start(tree.root)
        assert _session_start() == 0

        assert _dirs_in(tree.runs_dir / SLUG) == sorted(SESSION_DIR_NAMES)
        state = run_state.load_state(record)
        assert state["stop_reason"] is None
        assert [event["type"] for event in run_state.read_events(record, last_n=0)][
            -3:
        ].count("question_answered") == 1


class TestADotLeadingIdOpensNoInvisibleRecord:
    """A reserved name, and the one shape that used to succeed at being wrong.

    Every walk over a group skips dot-children by design — `run_state.
    _session_dirs` and `lmer_platform.workrepo._sessions_in` both do, because a
    dot-dir under a run area is a `.new-*` seed mid-rename and never a record.
    So `.hidden` did not FAIL to open a record: it opened one that was written,
    complete, claimed and then invisible to `work sessions`, to the fleet view,
    to `resolve_run_dir` and to every later resume, while the session itself saw
    nothing wrong (!268 review).

    That is worse than the quiet degradation the guards exist to produce, so all
    three copies of the rule now refuse it — the reviewer scoped the finding to
    the hand-typed host flag, but `work session-start --session .hidden` reaches
    the same address through the kernel's own explicit route, so a host-only fix
    would have left the defect in and called it an asymmetry.
    """

    BAD = ".hidden"

    def test_the_host_cli_refuses_it(self):
        from lmer_cli.cli import main as cli_main

        with patch.dict(os.environ, dict(_BASE_ENV), clear=True):
            with _make_main_mocks(captured_env={}):
                rc = cli_main(
                    ["--no-task", "--exec", "true", f"https://{PRIMARY_REPO}",
                     "--session", self.BAD]
                )
        assert rc == 2

    def test_a_hand_set_env_value_opens_no_record_of_its_own(
        self, monkeypatch, schema_tree
    ):
        """The container's degradation, which is the safe one: an unusable id
        is NO launch id, so the launch joins the group's newest record instead
        of founding one nothing can see."""
        _set_context(monkeypatch, schema_tree.root, self.BAD)
        _container_start(schema_tree.root)
        assert _session_start() == 0

        group = schema_tree.runs_dir / SLUG
        assert _dirs_in(group) == sorted(SESSION_DIR_NAMES)
        assert not (group / self.BAD).exists()
        assert run_state.run_dir() == group / RESUMED_DIR_NAME

    def test_the_explicit_kernel_route_refuses_it_too(
        self, monkeypatch, schema_tree
    ):
        """`work session-start --session .hidden` — the route the review's
        scoping note did not cover, and the one that made this a three-place
        fix rather than a host-only one. Hook-facing, so it is reported and
        still exits 0; what matters is that no record is created."""
        _set_context(monkeypatch, schema_tree.root, SESSION_IDS[0])
        with patch.object(
            work_cli.sys, "argv", ["work", "session-start", "--session", self.BAD]
        ):
            assert work_cli.main() == 0

        group = schema_tree.runs_dir / SLUG
        assert _dirs_in(group) == sorted(SESSION_DIR_NAMES)
        assert not (group / self.BAD).exists()
