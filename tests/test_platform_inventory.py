"""Tests for the run-centric inventory (issue #141, slice M1 / T5).

Covers every run state in spec §5.4, the attention axis being independent of
liveness, and the two judgement calls the module documents: a crash needs
evidence (a stale registry entry), and a session with no run dir yet still gets
a row.
"""

import json
import os

import pytest

from lmer_platform import assistant, inventory, meta, registry, runs, store
from lmer_platform.workrepo import RunDirRef
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    """Somewhere for ``registry.register`` to write, away from the real state dir."""
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def plant_run(tmp_path, slug, *, host="gitlab.example.com", project="agents/global",
              state_yaml=None, ledger=None, events=None):
    """Create a run dir on disk and return its ref."""
    path = tmp_path / host / project / "runs" / slug
    path.mkdir(parents=True, exist_ok=True)
    if state_yaml is not None:
        (path / "state.yaml").write_text(state_yaml, encoding="utf-8")
    if ledger is not None:
        (path / "ledger.yaml").write_text(ledger, encoding="utf-8")
    if events is not None:
        (path / "events.jsonl").write_text(events, encoding="utf-8")
    return RunDirRef(host=host, project=project, slug=slug, path=path)


def state_yaml(**fields):
    lines = ["schema: 1"]
    for key, value in fields.items():
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def session(slug, *, live=True, host="gitlab.example.com", project="agents/global", **extra):
    entry = {
        "id": f"s-{slug}",
        "kind": "worker",
        "pid": 4242,
        "live": live,
        "started_at": "2026-07-26T10:00:00Z",
        "run": {"host": host, "project": project, "slug": slug},
        "task": {"taskdef": "develop", "target": "issue-141"},
    }
    entry.update(extra)
    return entry


# --- state derivation -------------------------------------------------------

def test_live_session_is_running(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    inv = inventory.build_inventory([ref], [session("r1")])
    run = inv.runs[0]

    assert run.state == "running"
    assert run.live is True
    assert run.attention is None


def test_live_session_outranks_stale_committed_state(tmp_path):
    """Run state is git-eventual; liveness is immediate, so liveness wins."""
    ref = plant_run(
        tmp_path, "r1",
        state_yaml=state_yaml(status="in-progress", stop_reason="question",
                              open_question="answer me"),
    )
    inv = inventory.build_inventory([ref], [session("r1", live=True)])
    assert inv.runs[0].state == "running"


def test_question_with_no_session_waits_on_you(tmp_path):
    ref = plant_run(
        tmp_path, "r1",
        state_yaml=state_yaml(status="in-progress", stop_reason="question",
                             open_question="which approach?",
                             updated="2026-07-26T12:00:00Z"),
    )
    inv = inventory.build_inventory([ref], [])
    run = inv.runs[0]

    assert run.state == "waiting_on_you"
    assert run.live is False
    assert run.attention.reason == "question"
    assert run.attention.note == "which approach?"
    assert run.attention.since == "2026-07-26T12:00:00Z"


def test_question_without_recorded_text_says_so(tmp_path):
    """Runs in the wild stop on `question` without recording the text."""
    ref = plant_run(tmp_path, "r1",
                    state_yaml=state_yaml(status="in-progress",
                                          stop_reason="question"))
    run = inventory.build_inventory([ref], []).runs[0]

    assert run.state == "waiting_on_you"
    assert run.attention.reason == "question"
    assert "not recorded" in run.attention.note


def test_parked_run_has_no_attention(tmp_path):
    """Parked is waiting for a slot, not for you."""
    ref = plant_run(tmp_path, "r1",
                    state_yaml=state_yaml(status="in-progress", stop_reason="paused"))
    run = inventory.build_inventory([ref], []).runs[0]
    assert run.state == "parked"
    assert run.attention is None


def test_yielded_run_asks_for_review(tmp_path):
    ref = plant_run(tmp_path, "r1",
                    state_yaml=state_yaml(status="in-progress", stop_reason="yield"))
    run = inventory.build_inventory([ref], []).runs[0]
    assert run.state == "yielded"
    assert run.attention.reason == "yield"


def test_critical_error_surfaces_its_summary(tmp_path):
    ref = plant_run(
        tmp_path, "r1",
        state_yaml=(
            "schema: 1\nstatus: in-progress\nstop_reason: critical_error\n"
            "critical_error:\n  summary: disk full\n  detail: no space\n"
        ),
    )
    run = inventory.build_inventory([ref], []).runs[0]
    assert run.state == "failed"
    assert run.attention.reason == "critical_error"
    assert run.attention.note == "disk full"


@pytest.mark.parametrize("status", ["complete", "archived"])
def test_terminal_status_is_complete(tmp_path, status):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status=status,
                                                          stop_reason="complete"))
    run = inventory.build_inventory([ref], []).runs[0]
    assert run.state == "complete"
    assert run.attention is None


def test_in_progress_without_session_is_dormant_not_crashed(tmp_path):
    """The mirror is full of old in-progress runs; calling them crashed is noise."""
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    run = inventory.build_inventory([ref], []).runs[0]
    assert run.state == "dormant"
    assert run.attention is None


def test_stale_registry_entry_is_the_crash_evidence(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    run = inventory.build_inventory([ref], [session("r1", live=False)]).runs[0]

    assert run.state == "crashed"
    assert run.attention.reason == "crashed"
    assert run.attention.since == "2026-07-26T10:00:00Z"


def test_missing_state_file_is_dormant(tmp_path):
    ref = plant_run(tmp_path, "r1")
    assert inventory.build_inventory([ref], []).runs[0].state == "dormant"


def test_corrupt_state_is_isolated_to_its_own_run(tmp_path):
    """One bad state.yaml must not take out the inventory."""
    bad = plant_run(tmp_path, "bad", state_yaml="}{ not yaml [")
    good = plant_run(tmp_path, "good", state_yaml=state_yaml(status="in-progress"))

    inv = inventory.build_inventory([bad, good], [])
    by_slug = {r.slug: r for r in inv.runs}

    assert by_slug["bad"].state == "unknown"
    assert by_slug["bad"].attention.reason == "unreadable"
    assert by_slug["good"].state == "dormant"


def test_newer_schema_state_is_reported_not_fatal(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml="schema: 99\nstatus: in-progress\n")
    run = inventory.build_inventory([ref], []).runs[0]
    assert run.state == "unknown"
    assert "newer than supported" in run.attention.note


# --- run metadata -----------------------------------------------------------

def test_run_metadata_is_surfaced(tmp_path):
    ref = plant_run(
        tmp_path, "develop-issue-141",
        state_yaml=state_yaml(
            status="in-progress", phase="execution", name="lmer-orchestrator",
            taskdef="develop", target="https://gitlab.example.com/x/-/work_items/141",
            goal="build the thing", updated="2026-07-26T12:00:00Z",
        ),
    )
    run = inventory.build_inventory([ref], []).runs[0]

    assert run.label == "lmer-orchestrator"
    assert run.phase == "execution"
    assert run.taskdef == "develop"
    assert run.goal == "build the thing"
    assert run.rel_path == "gitlab.example.com/agents/global/runs/develop-issue-141"


def test_label_falls_back_to_slug(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    assert inventory.build_inventory([ref], []).runs[0].label == "r1"


def test_ports_come_from_the_session(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = session("r1", ports=[{"host": 30021, "container": 3000}])
    run = inventory.build_inventory([ref], [entry]).runs[0]
    assert run.ports == [{"host": 30021, "container": 3000}]


def test_ports_empty_without_session(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    assert inventory.build_inventory([ref], []).runs[0].ports == []


def test_ledger_summary_is_included(tmp_path):
    ref = plant_run(
        tmp_path, "r1",
        state_yaml=state_yaml(status="in-progress"),
        ledger=(
            "schema: 1\ntasks:\n"
            "  T1:\n    status: done\n    commit: abc123\n"
            "  T2:\n    status: in-progress\n"
        ),
    )
    run = inventory.build_inventory([ref], []).runs[0]
    assert run.ledger is not None
    assert run.ledger.get("done") == 1


def test_recent_events_are_included_and_bounded(tmp_path):
    events = "\n".join(
        f'{{"ts": "2026-07-26T10:0{i}:00Z", "type": "tick", "note": "{i}"}}'
        for i in range(5)
    )
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"),
                    events=events + "\n")
    run = inventory.build_inventory([ref], [], event_count=2).runs[0]
    assert [e["note"] for e in run.events] == ["3", "4"]


def test_events_can_be_disabled(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"),
                    events='{"ts": "x", "type": "tick"}\n')
    assert inventory.build_inventory([ref], [], event_count=0).runs[0].events == []


# --- sessions without run dirs ----------------------------------------------

def test_session_without_run_dir_still_appears(tmp_path):
    """A just-spawned session has not committed its run dir yet."""
    inv = inventory.build_inventory([], [session("brand-new")])
    run = inv.runs[0]

    assert run.slug == "brand-new"
    assert run.state == "running"
    assert run.taskdef == "develop"
    assert run.live is True


def test_dead_session_without_run_dir_is_crashed(tmp_path):
    inv = inventory.build_inventory([], [session("never-committed", live=False)])
    run = inv.runs[0]
    assert run.state == "crashed"
    assert "no run state was ever committed" in run.attention.note


def test_session_with_no_run_block_still_appears(tmp_path):
    entry = session("x")
    entry["run"] = {}
    inv = inventory.build_inventory([], [entry])
    assert len(inv.runs) == 1
    assert inv.runs[0].state == "running"


def test_live_session_wins_over_stale_one_for_the_same_run(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    dead = session("r1", live=False)
    dead["id"] = "s-old"
    alive = session("r1", live=True)
    alive["id"] = "s-new"

    inv = inventory.build_inventory([ref], [dead, alive])
    matched = [r for r in inv.runs if r.rel_path]
    assert len(matched) == 1
    assert matched[0].session["id"] == "s-new"
    assert matched[0].state == "running"
    # the superseded entry is not silently dropped
    assert len(inv.runs) == 2


def test_liveness_is_computed_when_absent_from_the_entry(tmp_path):
    """Entries read straight off disk have no computed `live` flag."""
    entry = session("r1")
    del entry["live"]
    entry["pid"] = 2**22  # certainly dead

    inv = inventory.build_inventory([], [entry])
    assert inv.runs[0].state == "crashed"


def test_non_dict_sessions_are_ignored(tmp_path):
    inv = inventory.build_inventory([], ["nonsense", None, 42])
    assert inv.runs == []


# --- aggregation and ordering -----------------------------------------------

def test_counts_by_state(tmp_path):
    refs = [
        plant_run(tmp_path, "a", state_yaml=state_yaml(status="in-progress")),
        plant_run(tmp_path, "b", state_yaml=state_yaml(status="complete")),
        plant_run(tmp_path, "c", state_yaml=state_yaml(status="in-progress",
                                                       stop_reason="question")),
    ]
    inv = inventory.build_inventory(refs, [])
    assert inv.counts() == {"dormant": 1, "complete": 1, "waiting_on_you": 1}


def test_attention_list_orders_question_before_crash(tmp_path):
    crashed = plant_run(tmp_path, "crashed",
                        state_yaml=state_yaml(status="in-progress"))
    asking = plant_run(tmp_path, "asking",
                       state_yaml=state_yaml(status="in-progress",
                                             stop_reason="question"))
    inv = inventory.build_inventory(
        [crashed, asking], [session("crashed", live=False)]
    )
    assert [r.attention.reason for r in inv.attention] == ["question", "crashed"]


def test_sorted_runs_puts_attention_first_then_live(tmp_path):
    refs = [
        plant_run(tmp_path, "idle", state_yaml=state_yaml(status="complete",
                                                          updated="2026-07-26T09:00:00Z")),
        plant_run(tmp_path, "busy", state_yaml=state_yaml(status="in-progress",
                                                          updated="2026-07-26T08:00:00Z")),
        plant_run(tmp_path, "asking", state_yaml=state_yaml(status="in-progress",
                                                            stop_reason="question",
                                                            updated="2026-07-26T07:00:00Z")),
    ]
    inv = inventory.build_inventory(refs, [session("busy")])
    assert [r.slug for r in inv.sorted_runs()] == ["asking", "busy", "idle"]


def test_sorted_runs_breaks_ties_by_most_recent_update(tmp_path):
    refs = [
        plant_run(tmp_path, "older", state_yaml=state_yaml(status="complete",
                                                           updated="2026-07-20T00:00:00Z")),
        plant_run(tmp_path, "newer", state_yaml=state_yaml(status="complete",
                                                           updated="2026-07-26T00:00:00Z")),
    ]
    inv = inventory.build_inventory(refs, [])
    assert [r.slug for r in inv.sorted_runs()] == ["newer", "older"]


def test_runs_without_update_time_sort_last(tmp_path):
    refs = [
        plant_run(tmp_path, "undated", state_yaml=state_yaml(status="complete")),
        plant_run(tmp_path, "dated", state_yaml=state_yaml(status="complete",
                                                           updated="2026-07-20T00:00:00Z")),
    ]
    inv = inventory.build_inventory(refs, [])
    assert [r.slug for r in inv.sorted_runs()] == ["dated", "undated"]


def test_to_dict_shape(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress",
                                                          stop_reason="question"))
    payload = inventory.build_inventory([ref], []).to_dict()

    assert payload["totals"] == {"runs": 1, "live": 0, "attention": 1}
    assert payload["counts"] == {"waiting_on_you": 1}
    assert payload["runs"][0]["attention"]["reason"] == "question"
    # 1, not 0: a *live* session waiting on its ask channel takes the top slot
    # (T23) — it is up and idle, holding a slot, where this run has exited.
    assert payload["runs"][0]["attention"]["priority"] == 1
    assert payload["attention"][0]["slug"] == "r1"


# --- tracked-run scoping (D25) ----------------------------------------------

class _Tracked:
    """Stand-in for lmer_platform.runs.TrackedRun.

    Carries no ``rel_path``, because the real entry carries none: the index knows
    the slug, and a named run's directory is ``runs/<slug>--<name>`` (T96). A stub
    that offered one would keep a composed path reaching the row long after the
    thing it stands in for stopped composing one.
    """

    def __init__(self, slug, *, host="gitlab.example.com", project="agents/global",
                 taskdef=None, target=None, last_seen=None, last_session_id=None):
        self.host = host
        self.project = project
        self.slug = slug
        self.taskdef = taskdef
        self.target = target
        self.last_seen = last_seen
        self.last_session_id = last_session_id
        self.first_seen = last_seen
        self.key = f"{host}/{project}/{slug}"


def test_tracked_run_without_a_dir_gets_a_row(tmp_path):
    """Tracked but unpushed: a row beats an absence indistinguishable from untracked."""
    inv = inventory.build_inventory(
        [], [], tracked=[_Tracked("not-pushed", taskdef="develop", target="t")]
    )
    run = inv.runs[0]

    assert run.slug == "not-pushed"
    assert run.state == "dormant"
    assert run.taskdef == "develop"
    # No address, for the reason this row exists at all: nothing found a directory
    # for this run, and `runs/<slug>` is not one for any run with a name (T96). The
    # row still says which run it is — host, project and slug are on it — and a
    # client renders a path only when there is one, as it does for the row of a
    # session whose first commit has not landed.
    assert run.rel_path is None, "the row invents a directory nobody found"


def test_tracked_run_with_a_dir_uses_the_dir(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress",
                                                          phase="execution"))
    inv = inventory.build_inventory([ref], [], tracked=[_Tracked("r1")])

    assert len(inv.runs) == 1
    assert inv.runs[0].phase == "execution"


def test_tracked_run_with_a_session_but_no_dir_keeps_the_session_row(tmp_path):
    """Regression guard: the session row must not be swallowed by the tracked row."""
    inv = inventory.build_inventory(
        [], [session("fresh")], tracked=[_Tracked("fresh", taskdef="develop")]
    )

    assert len(inv.runs) == 1
    assert inv.runs[0].state == "running"
    assert inv.runs[0].session is not None


def test_tracked_none_means_no_scoping(tmp_path):
    """Unit callers pass nothing and get exactly the refs and sessions given."""
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    assert len(inventory.build_inventory([ref], [], tracked=None).runs) == 1


def test_empty_inventory(tmp_path):
    payload = inventory.build_inventory([], []).to_dict()
    assert payload["totals"] == {"runs": 0, "live": 0, "attention": 0}
    assert payload["runs"] == []


def test_every_declared_state_is_reachable_or_documented():
    """Guard against the vocabulary drifting from what the code can produce."""
    assert set(inventory.ATTENTION_PRIORITY) <= set(inventory.ATTENTION_REASONS)
    for later_slice in ("held", "feedback"):
        assert later_slice in inventory.RUN_STATES


# --- terminal history after a clean exit ------------------------------------
#
# A clean exit removes the registry entry, so `session` goes None while the PTY
# log deliberately remains (spec D16). Without an id from the tracked index the
# UI has nothing to ask the log endpoint for, and a finished run's history would
# be unreachable — which defeats the reason the log outlives the container.

def test_live_session_id_is_used_for_history(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    run = inventory.build_inventory([ref], [session("r1")]).runs[0]

    assert run.session_id_for_history == "s-r1"
    assert run.to_dict()["last_session_id"] == "s-r1"


def test_finished_run_still_offers_its_last_session(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="complete"))
    run = inventory.build_inventory(
        [ref], [], tracked=[_Tracked("r1", last_session_id="s-gone")]
    ).runs[0]

    assert run.session is None, "a cleanly exited session leaves no registry entry"
    assert run.session_id_for_history == "s-gone"
    assert run.to_dict()["last_session_id"] == "s-gone"


def test_a_live_entry_outranks_the_remembered_id(tmp_path):
    """The running session is the one to attach to, not the previous incarnation."""
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    run = inventory.build_inventory(
        [ref], [session("r1")], tracked=[_Tracked("r1", last_session_id="s-old")]
    ).runs[0]
    assert run.session_id_for_history == "s-r1"


def test_no_history_id_when_nothing_ever_ran(tmp_path):
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    run = inventory.build_inventory([ref], [], tracked=[_Tracked("r1")]).runs[0]
    assert run.session_id_for_history is None
    assert run.to_dict()["last_session_id"] is None


def test_tracked_run_without_a_dir_carries_its_last_session(tmp_path):
    run = inventory.build_inventory(
        [], [], tracked=[_Tracked("not-pushed", last_session_id="s-1")]
    ).runs[0]
    assert run.session_id_for_history == "s-1"


# --- how a run was launched (T50) -------------------------------------------
#
# The preset, the fan-out selection and the harness are recorded by
# lmer_platform.spawn on the session's registry entry and *nowhere else*: the run
# state schema has no field for any of them and the tracked index does not carry
# them. So the row can report them exactly as long as that entry exists, which is
# a real limit on what the fleet view can say and is pinned here rather than
# discovered by an operator wondering why a dormant run lost its preset.

def test_the_launch_selection_reaches_the_fleet_payload(tmp_path):
    """What the spawn recorded has to survive as far as the row.

    Flat fields, not a client digging through ``session["task"]``: that block is
    the registry entry verbatim, and a UI reading into it would be coupled to the
    spawn record's shape.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = session("r1")
    entry["task"] = {
        "taskdef": "review", "target": "t", "preset": "sol-review",
        "agents": "fable,sol", "harness": "codex",
    }
    run = inventory.build_inventory([ref], [entry]).runs[0]

    assert run.preset == "sol-review"
    assert run.agents == "fable,sol"
    assert run.harness == "codex"
    payload = run.to_dict()
    assert payload["preset"] == "sol-review"
    assert payload["agents"] == "fable,sol"
    assert payload["harness"] == "codex"


def test_a_just_spawned_session_already_says_how_it_was_launched(tmp_path):
    """The row that exists *because* there is no run dir yet still carries it.

    A session's first minutes are when "which of these did I start with the
    review preset?" is asked most, and that row is built from the entry alone.
    """
    entry = session("brand-new")
    entry["task"] = {"taskdef": "review", "preset": "sol-review", "agents": "sol"}
    run = inventory.build_inventory([], [entry]).runs[0]

    assert run.rel_path is None, "this is the no-run-dir row"
    assert run.preset == "sol-review"
    assert run.agents == "sol"


def test_a_run_whose_session_is_gone_admits_it_knows_no_preset(tmp_path):
    """The limit, stated: a clean exit takes the only record of it with it.

    Not a bug to route around here — inferring a preset from anything else would
    be inventing one — but the reason a client must render these only when
    present instead of holding a slot open for them.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="complete"))
    run = inventory.build_inventory([ref], []).runs[0]

    assert run.session is None
    assert (run.preset, run.agents, run.harness) == (None, None, None)


def test_a_spawn_that_named_no_harness_reports_none_rather_than_a_guess(tmp_path):
    """``None`` is not "the default one".

    With no ``--harness`` the harness is resolved inside the session (from
    ``LMER_HARNESS``, then the model hint in ``LMER_LLM_NAME``), and the host
    never learns which won. Naming a default here would put "claude" on the row
    of every session a preset started as codex.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = session("r1")
    entry["task"] = {"taskdef": "develop", "target": "t", "harness": None}
    assert inventory.build_inventory([ref], [entry]).runs[0].harness is None


def test_a_session_that_never_reported_a_model_says_so_rather_than_guessing(tmp_path):
    """Renamed: T51 landed the reporting, so "nothing records it yet" is no longer
    true and a test asserting that would be documenting a fact that has changed.

    What survives is the case that still matters and always will — a session that
    resolved no model, or one predating the reporting. The row must say nothing
    rather than infer one, because the daemon's own environment is not evidence
    about a run: an exported ``LMER_LLM_NAME`` beats a preset's value host-side,
    so a guess would be wrong for exactly the runs that name a preset.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = session("r1")
    entry["task"] = {
        "taskdef": "develop", "target": "t", "preset": "opus-review",
        "agents": "fable", "harness": "claude",
    }
    run = inventory.build_inventory([ref], [entry]).runs[0]

    assert run.model is None, "a model was invented for a session that reported none"
    assert "model" in run.to_dict(), (
        "no field for the model at all means a client has to invent a name for "
        "it, and two clients would invent two"
    )


def test_a_task_block_the_daemon_did_not_write_costs_only_its_own_metadata(tmp_path):
    """An entry from another version must not take the row down with it.

    Same tolerance the module gives an unreadable ``state.yaml``: one run's
    metadata is worth less than the fleet view.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    for task in (None, "develop", ["develop"], 7):
        entry = session("r1")
        entry["task"] = task
        run = inventory.build_inventory([ref], [entry]).runs[0]
        assert run.state == "running", f"a task block of {task!r} broke the row"
        assert (run.preset, run.agents, run.harness, run.model) == (None,) * 4


# --- how much of the session entry crosses (T57) ------------------------------
#
# The row reads the whole registry entry — liveness, ports, the detached record
# and the launch selection all come out of it — and *ships* a projection of it.
# The entry is a host-side record: it names a control-plane token file, a
# container, a Slack thread and where things sit on this machine's disk, none of
# which a browser has anything to do with. Same rule
# lmer_platform.transcripts.Source applies to its `path`, for the same reason.
#
# An allowlist, and that is the part these tests are really about. The block used
# to be the entry as read, so a key written onto an entry by any part of the
# platform arrived in a client with nobody deciding it should — and the entry has
# grown keys twice since (the detached record, the lifecycle record). Narrowing
# fixes one payload; the allowlist is what stops the next key repeating it.

#: Every allowlisted field and what reads it. Pinned here so widening the list
#: means naming a reader — a field nobody reads is one nobody decided to share.
SESSION_FIELD_READERS = {
    "id": "RunDetail.vue: the live-session card, and the wind-down/exit verbs",
    "pid": "RunDetail.vue: the live-session card",
    "started_at": "RunDetail.vue: 'started <n> ago'",
    "log_path": "RunDetail.vue: where the PTY log is on the host",
    "lifecycle": "RunDetail.vue: the recorded wind-down request",
    "activity": "RunCard.vue: 'idle 22m' beside the run's age (idle_seconds), "
                "with last_output_at as its tooltip",
}

#: Stands in for the token file's path, which is what the entry carries — the
#: token itself is never in a registry file (registry._reject_inline_token).
TOKEN_REF = "/home/dev/.lmer/platform/sessions/s-full.token"


def full_entry(**extra):
    """A registry entry carrying every key the platform is known to write on one.

    Built through ``registry.register`` rather than by hand so the fixed part of
    the shape comes from the code that writes it, plus the three keys added to an
    entry *after* registration, by name. Nothing here has to be listed anywhere
    for the payload to stay narrow — that is the property under test.
    """
    entry = registry.register(
        "s-full",
        pid=os.getpid(),
        run={"host": "gitlab.example.com", "project": "agents/global", "slug": "r1"},
        task={"taskdef": "develop", "target": "issue-141", "preset": "sol-review",
              "agents": "fable,sol", "harness": "codex"},
        control={"host": "127.0.0.1", "port": 8931, "token_ref": TOKEN_REF},
        transcript={"path": "/home/dev/.claude/projects/-work/abc.jsonl"},
        container_id="9f3ca1b2c3d4",
        slot="slot-1",
        ports=[{"host": 30021, "container": 3000}],
        log_path="/home/dev/.lmer/platform/logs/s-full.log",
        slack={"channel": "C0123456789", "thread_ts": "1720000000.000100"},
    )
    entry["live"] = True  # registry.list_sessions computes it on read
    entry["detached"] = {"output": "control_plane", "reason": "daemon_restart"}
    entry["lifecycle"] = {"verb": "wind_down", "requested_at": "2026-07-27T09:00:00Z"}
    entry.update(extra)
    return entry


def test_a_registry_key_invented_tomorrow_does_not_reach_a_client(tmp_path, platform_root):
    """The load-bearing one: the block must not widen on its own.

    Not "today's entry no longer ships today's extra keys" — that is an instance,
    and fixing an instance leaves the shape that produced it. What has to hold is
    that a key nobody here has heard of, written onto an entry by some later
    slice, stays host-side until someone puts it on the list deliberately.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = full_entry(
        a_field_no_one_has_thought_of_yet="host-side business",
        credentials_v2={"token_ref": "/run/secrets/later"},
    )

    payload = inventory.build_inventory([ref], [entry]).runs[0].to_dict()

    unlisted = set(payload["session"]) - set(inventory.SESSION_FIELDS)
    assert unlisted == set(), (
        f"{sorted(unlisted)} crossed into the fleet payload without being added "
        "to inventory.SESSION_FIELDS — the session block is an allowlist, so a "
        "new key on a registry entry has to be put on it on purpose"
    )


def test_the_control_token_ref_and_the_host_layout_stay_host_side(tmp_path, platform_root):
    """The instance that started this, stated so a revert names it.

    ``token_ref`` is a path to a mode-0600 file rather than a credential, and the
    client is already authenticated — so this is need-to-know, not a hole. It is
    still nothing the browser can use, and the same goes for the container id, the
    Slack thread and where the transcript sits on this machine.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = full_entry()

    payload = inventory.build_inventory([ref], [entry]).runs[0].to_dict()

    for key in ("control", "transcript", "container_id", "slack", "slot",
                "owner_pid", "schema", "run", "task"):
        assert key not in payload["session"], f"{key} is not the browser's business"
    assert TOKEN_REF not in json.dumps(payload), (
        "the control-plane token file's path reached the row by some other route"
    )


def test_the_detail_view_still_gets_every_field_it_reads(tmp_path, platform_root):
    """Narrowing must not quietly empty the live-session card.

    The other direction of the same guard: a field dropped from the allowlist is
    a blank in the UI with nothing anywhere saying why.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = full_entry()

    block = inventory.build_inventory([ref], [entry]).runs[0].to_dict()["session"]

    assert block["id"] == "s-full"
    assert block["pid"] == os.getpid()
    assert block["started_at"] == entry["started_at"]
    assert block["log_path"] == "/home/dev/.lmer/platform/logs/s-full.log"
    assert block["lifecycle"]["verb"] == "wind_down"


def test_every_allowlisted_field_names_the_code_that_reads_it():
    """Widening the list is a deliberate act: name the reader or leave it off."""
    assert set(inventory.SESSION_FIELDS) == set(SESSION_FIELD_READERS), (
        "a field was added to or removed from inventory.SESSION_FIELDS without "
        "saying which part of the UI reads it"
    )


def test_a_run_with_no_session_says_null_rather_than_an_empty_object(tmp_path):
    """``{}`` is truthy in JavaScript, and the client tests this object.

    A dormant run answering with an empty mapping would put a blank live-session
    card on every row that has nothing running.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="complete"))
    assert inventory.build_inventory([ref], []).runs[0].to_dict()["session"] is None


def test_a_field_the_entry_never_carried_is_present_and_null(tmp_path):
    """The keys a client sees do not depend on how much the daemon had learned.

    ``log_path`` and ``lifecycle`` are both written after the spawn, so an entry
    read in between has neither; omitting them would make the block's shape a
    function of timing. ``activity`` is never on the entry at all — it is folded on
    while the row is built, and is absent for every session that cannot be asked.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    block = inventory.build_inventory([ref], [session("r1")]).runs[0].to_dict()["session"]

    assert set(block) == set(inventory.SESSION_FIELDS)
    assert block["log_path"] is None
    assert block["lifecycle"] is None
    assert block["activity"] is None


def test_the_row_still_reports_what_it_read_out_of_the_entry(tmp_path, platform_root):
    """Nothing the fleet view showed is lost by not shipping the entry.

    Everything a client had to dig for is a field of its own — which is what made
    narrowing safe, and is the thing to re-check before adding to the allowlist:
    the answer to "the UI needs X" is a documented member, not the whole record.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    payload = inventory.build_inventory([ref], [full_entry()]).runs[0].to_dict()

    assert payload["live"] is True
    assert payload["ports"] == [{"host": 30021, "container": 3000}]
    assert payload["detached"]["output"] == "control_plane"
    assert payload["last_session_id"] == "s-full"
    assert (payload["preset"], payload["agents"], payload["harness"]) == (
        "sol-review", "fable,sol", "codex"
    )


def test_a_reported_model_reaches_the_row(tmp_path):
    """The other half, now that T51 made it reachable.

    The session resolves the model itself and reports it late through the ports
    file; `absorb_ports` folds it into the entry's task block. Without this test
    the pair would only assert the absent case, which is how a field that stopped
    being populated would keep passing.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = session("r1")
    entry["task"] = {
        "taskdef": "develop", "target": "t", "preset": "opus-review",
        "agents": "fable", "harness": "codex", "model": "gpt-5.6-sol",
    }
    run = inventory.build_inventory([ref], [entry]).runs[0]

    assert run.model == "gpt-5.6-sol"
    assert run.to_dict()["model"] == "gpt-5.6-sol"


# --- what this orchestrator calls a run (T65) ---------------------------------
#
# The title is the one field on a row that is not the run's: it is this
# orchestrator's note about it (lmer_platform.meta), stored in its own snapshot
# and joined on here. The operator asked: "if meta.title is set it should be used in
# the details header and the listings" — a title only visible inside its own tab
# cannot be what an operator identifies a run by, which is the whole reason T52
# stored one.
#
# Three properties are what make joining it safe, and each has a test below:
#
# - the storage does not move. Writing a title onto the tracked index would
#   refresh `last_seen`, which is what orders the fleet, so renaming a finished
#   run would shove it to the top as if it had just done something — and a lost
#   update in runs.json costs a whole run rather than a title. So the read path
#   reads run_meta.json and never writes the index;
# - it is one read for the whole fleet, not one per run and not one request per
#   row from the browser. The landing screen is every tracked run;
# - a snapshot that cannot be read costs the titles and nothing else. Every row
#   falls back to the label, which is the run's own name and always there.

def plant_titles(entries):
    """Write ``run_meta.json`` the way :mod:`lmer_platform.meta` writes it.

    Through ``store.write_json`` rather than by hand so the file's envelope (the
    schema stamp the reader checks) comes from the code that stamps it — a
    hand-rolled copy here would pass while the real format moved on. Each record
    is planted verbatim, junk included: what one version wrote and another reads
    is exactly the case the tolerance below is for.
    """
    payload = {
        runs.run_key("gitlab.example.com", "agents/global", slug): record
        for slug, record in entries.items()
    }
    store.write_json(store.snapshot_path(meta.META_FILE), {"meta": payload})


def test_a_title_this_orchestrator_wrote_reaches_the_fleet_row(tmp_path, platform_root):
    """The point of the whole slice: the title is on the row, beside the label.

    Beside and not instead: ``label`` is the run's own name and stays the fallback
    for the runs — most of them — that nobody has described.
    """
    ref = plant_run(tmp_path, "develop-issue-141",
                    state_yaml=state_yaml(status="in-progress", name="orchestrator"))
    plant_titles({"develop-issue-141": {"title": "wire the fleet view to titles"}})

    run = inventory.build_inventory([ref], []).runs[0]

    assert run.title == "wire the fleet view to titles"
    assert run.label == "orchestrator", "the title swallowed the run's own name"
    payload = run.to_dict()
    assert payload["title"] == "wire the fleet view to titles"
    assert payload["label"] == "orchestrator"


def test_a_run_nobody_has_described_carries_no_title_and_keeps_its_label(
    tmp_path, platform_root,
):
    """The common case, and the reason the field is nullable rather than a string.

    A client renders the title where a run is named and falls back to the label,
    so "no title" has to be falsy and the label has to be there — a row that
    answered with a name-shaped blank would be a heading with nothing in it.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    plant_titles({"somebody-else": {"title": "not this run"}})

    run = inventory.build_inventory([ref], []).runs[0]

    assert run.title is None
    assert run.to_dict()["title"] is None
    assert run.label == "r1"


def test_a_named_runs_row_needs_no_re_keying(tmp_path, platform_root):
    """The consumer half of T90: a run whose directory the container renamed to
    ``<slug>--<name>`` joins its tracked entry, its session and its title exactly
    as it always did, because the correction is to the run's *address* and never
    to its identity (:func:`lmer_platform.workrepo.resolve_run_dir`).

    This is what a re-key would have cost: the tracked index, the title, the
    session entry (which the next spawn writes from ``derive_run_identity``, i.e.
    under the slug again) and the run-files route all key on the slug, so moving
    the key would have had to move four things and would have come back apart on
    the next respawn.
    """
    ref = plant_run(
        tmp_path, "review-mr-172--review-mr-172",
        state_yaml=state_yaml(
            slug="review-mr-172", name="review-mr-172", status="in-progress",
            phase="cleanup",
        ),
    )
    ref = RunDirRef(
        host=ref.host, project=ref.project, slug="review-mr-172",
        path=ref.path, dir_name="review-mr-172--review-mr-172",
    )
    plant_titles({"review-mr-172": {"title": "review MR !172"}})

    inv = inventory.build_inventory(
        [ref], [session("review-mr-172")], tracked=[_Tracked("review-mr-172")]
    )

    assert len(inv.runs) == 1, "one run must not show up as two rows"
    row = inv.runs[0]
    assert row.slug == "review-mr-172"
    assert row.phase == "cleanup", "the row must carry the run's real state"
    assert row.state == "running"
    assert row.title == "review MR !172"
    assert row.rel_path == (
        "gitlab.example.com/agents/global/runs/review-mr-172--review-mr-172"
    ), "the path an operator opens is the directory that exists"


def test_re_keying_a_named_run_onto_its_dir_would_split_the_row(tmp_path,
                                                                platform_root):
    """The cost of the other fix, pinned so nobody has to rediscover it.

    Had the correction moved the run's *key* to its directory name instead of its
    address, this is what the fleet view would have served until every consumer had
    been migrated behind it: two rows for one run, and the run's state and the
    operator's own title for it on different ones — the run dir keyed on the
    directory carries the phase and no title, while the tracked entry still keyed
    on the slug carries the title and knows nothing about the run.
    """
    ref = plant_run(
        tmp_path, "review-mr-172--review-mr-172",
        state_yaml=state_yaml(slug="review-mr-172", status="in-progress",
                              phase="cleanup"),
    )
    plant_titles({"review-mr-172": {"title": "review MR !172"}})

    inv = inventory.build_inventory([ref], [], tracked=[_Tracked("review-mr-172")])

    assert len(inv.runs) == 2
    from_dir = next(run for run in inv.runs if run.phase == "cleanup")
    from_index = next(run for run in inv.runs if run.phase is None)
    assert from_dir.title is None, "the title is filed under the slug"
    assert from_index.title == "review MR !172"
    assert from_index.state == "dormant", "a live run showing as dormant, twice over"


def test_a_cleared_title_reads_the_same_as_one_never_written(tmp_path, platform_root):
    """One state on the row, as it is one state on disk.

    ``meta.write`` removes an entry whose fields are both empty, but a hand-edited
    or half-cleared file can still hold ``title: ""`` — and an empty string that
    reached the row as a title would be a heading rendering nothing at all rather
    than falling back to the label.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    plant_titles({"r1": {"title": "", "description": "still described, not named"}})

    assert inventory.build_inventory([ref], []).runs[0].title is None


def test_the_titles_are_one_read_for_the_whole_fleet(
    tmp_path, platform_root, monkeypatch,
):
    """The performance property, and it is the load-bearing one on this screen.

    The fleet view is every tracked run and it is polled from a phone. A read per
    row — here or, worse, a request per row from the browser — turns one small
    file into one file open per run on the first screen the app draws, for a label.

    Counted rather than timed: a per-run read is invisible in a test that only
    checks the titles arrived.
    """
    refs = [
        plant_run(tmp_path, slug, state_yaml=state_yaml(status="in-progress"))
        for slug in ("a", "b", "c")
    ]
    plant_titles({slug: {"title": f"title for {slug}"} for slug in ("a", "b", "c")})

    reads = []
    real_load_all = meta.load_all

    def counting_load_all():
        reads.append(1)
        return real_load_all()

    monkeypatch.setattr(meta, "load_all", counting_load_all)

    inv = inventory.build_inventory(
        refs, [session("b")], tracked=[_Tracked("a"), _Tracked("not-pushed")]
    )

    assert len(reads) == 1, (
        f"the titles were read {len(reads)} times for a fleet of {len(inv.runs)} "
        "rows; the snapshot holds every run's title, so the read belongs in the "
        "pass that builds the view and not in the row builders"
    )
    assert {r.slug: r.title for r in inv.runs if r.title} == {
        "a": "title for a", "b": "title for b", "c": "title for c",
    }


def test_a_titles_snapshot_that_cannot_be_read_costs_only_the_titles(
    tmp_path, platform_root,
):
    """A note about a run must never be able to take the fleet view down.

    Same discipline the module gives a corrupt ``state.yaml`` and a wrong-shaped
    session entry: the failure is worth less than the view. The rows come back
    with their labels, which is what every surface falls back to.
    """
    (platform_root).mkdir(parents=True, exist_ok=True)
    (platform_root / meta.META_FILE).write_text("}{ not json", encoding="utf-8")

    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress",
                                                          name="orchestrator"))
    run = inventory.build_inventory([ref], []).runs[0]

    assert run.state == "dormant", "an unreadable titles file broke the row"
    assert run.title is None
    assert run.label == "orchestrator"


def test_a_malformed_title_entry_costs_only_that_runs_title(tmp_path, platform_root):
    """One hand-edited entry, not the file.

    ``meta.RunMeta.from_dict`` is what tolerates this, and reading titles through
    it here rather than reaching into the raw mapping is what makes the tolerance
    apply on this path too.
    """
    refs = [
        plant_run(tmp_path, slug, state_yaml=state_yaml(status="in-progress"))
        for slug in ("bad", "worse", "good")
    ]
    plant_titles({
        "bad": {"title": ["a", "list"]},
        "worse": "not even a record",
        "good": {"title": "readable"},
    })

    by_slug = {r.slug: r for r in inventory.build_inventory(refs, []).runs}

    assert by_slug["bad"].title is None
    assert by_slug["worse"].title is None
    assert by_slug["good"].title == "readable"


def test_reading_the_titles_never_writes_the_tracked_index(tmp_path, platform_root):
    """T52's storage decision, pinned from the read side.

    The cheap version of this feature keeps the title on the tracked run, and the
    reason it was rejected has nothing to do with reading: ``track()`` refreshes
    ``last_seen``, which is what orders the fleet, so renaming a finished run
    would shove it to the top as if it had just done something — and a lost update
    in ``runs.json`` costs a run rather than a title. This test fails the day a
    join turns into a migration.
    """
    index = store.snapshot_path(runs.RUNS_FILE)
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    plant_titles({"r1": {"title": "a name for it"}})
    store.write_json(index, {"runs": {}})
    before = index.read_bytes()

    assert inventory.build_inventory([ref], []).runs[0].title == "a name for it"
    assert index.read_bytes() == before, (
        "building the fleet view wrote the tracked index — the titles live in "
        "their own snapshot precisely so a rename cannot touch it"
    )


def test_a_just_spawned_session_row_already_carries_its_title(tmp_path, platform_root):
    """The row that exists *because* there is no run dir yet needs it most.

    An orchestrator that spawns a run and titles it in the same breath is the
    likeliest author of a title, and this is the row those first minutes get. It
    is also the row with the least to identify it — no state, no phase, a slug and
    a taskdef.
    """
    plant_titles({"brand-new": {"title": "the run I just started"}})

    run = inventory.build_inventory([], [session("brand-new")]).runs[0]

    assert run.rel_path is None, "this is the no-run-dir row"
    assert run.title == "the run I just started"


def test_a_tracked_run_with_no_dir_and_no_session_still_carries_its_title(
    tmp_path, platform_root,
):
    """The third row builder. Three ways to make a row is three ways to lose this.

    A run tracked but not yet pushed shows what the index knows, and the index
    knows a taskdef and a target. The title is the only thing on that row written
    by a human about the run.
    """
    plant_titles({"not-pushed": {"title": "adopted, still on the laptop"}})

    run = inventory.build_inventory(
        [], [], tracked=[_Tracked("not-pushed", taskdef="develop")]
    ).runs[0]

    assert run.title == "adopted, still on the laptop"


def test_a_session_entry_that_names_no_run_gets_no_title_rather_than_an_error(
    tmp_path, platform_root,
):
    """``run_key`` refuses an incomplete triple, and a refusal is not a row's death.

    A registry entry with an empty ``run`` block already gets a row (a session
    whose spawn recorded nothing yet). Looking a title up for it has to answer
    "none", because raising here would take out the one row that is hardest to
    reach any other way.
    """
    plant_titles({"r1": {"title": "not reachable from that entry"}})
    entry = session("x")
    entry["run"] = {}

    run = inventory.build_inventory([], [entry]).runs[0]

    assert run.state == "running"
    assert run.title is None


def test_a_caller_holding_the_titles_can_pass_them_instead(tmp_path, platform_root):
    """The seam, and what ``{}`` means.

    ``None`` means "read the snapshot" — which is what makes every caller's payload
    carry the field without wiring — while a caller that already read the mapping
    passes it, and ``{}`` asks for rows with no titles at all. The default and the
    override must not be the same value, or "no titles" would silently mean "go and
    read some".
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    plant_titles({"r1": {"title": "what is on disk"}})

    passed = inventory.build_inventory(
        [ref], [], titles={runs.run_key("gitlab.example.com", "agents/global", "r1"):
                           "what the caller holds"},
    ).runs[0]
    assert passed.title == "what the caller holds"

    assert inventory.build_inventory([ref], [], titles={}).runs[0].title is None
    assert inventory.build_inventory([ref], []).runs[0].title == "what is on disk"


def test_the_title_does_not_cross_through_the_session_block(tmp_path, platform_root):
    """Where the field crosses, stated: a row field, not a widened allowlist.

    ``SESSION_FIELDS`` is the projection of a *registry entry* (T57), and the title
    is not on one — it is platform state about the run, keyed like the tracked
    index. Adding it there would put this orchestrator's note inside the block a
    client reads to ask "what is running", and would widen an allowlist whose whole
    job is to stay narrow. So the guard is that it did not happen.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    plant_titles({"r1": {"title": "a name for it"}})

    payload = inventory.build_inventory([ref], [full_entry()]).runs[0].to_dict()

    assert payload["title"] == "a name for it", "the title is not a top-level field"
    assert "title" not in inventory.SESSION_FIELDS
    assert "title" not in payload["session"], (
        "the title crossed inside the session block, which is a projection of the "
        "registry entry and has nothing to do with a note about the run"
    )


# --- which row is the platform itself (T85) -----------------------------------

def orchestrator_session(**extra):
    """The registry entry the platform's own session gets (``lmer_platform.assistant``).

    Its ``run`` block is empty because that session has no repository (spec D17), so
    nothing joins it to a run dir and the row is built from the entry alone — which
    is the shape the mark has to work on. Everything else about it is an ordinary
    entry, which is the point: ``kind`` is the only thing that says what it is.
    """
    entry = {
        "id": "s-uber",
        "kind": assistant.KIND,
        "pid": 4242,
        "live": True,
        "started_at": "2026-07-26T09:00:00Z",
        "run": {"host": None, "project": None, "slug": None},
        "task": {"taskdef": assistant.TASKDEF, "target": assistant.TARGET},
    }
    entry.update(extra)
    return entry


def test_the_orchestrators_own_row_says_it_is_the_orchestrator(platform_root):
    """The operator asked: "the orchestrator run needs to be clearly marked that it
    is that".

    Both listings and the detail view read one boolean off the row, so the daemon
    has to put it there — and it has to be beside the identity the listing already
    shows, because a badge on a row that names nothing is a badge on a mystery.
    """
    payload = inventory.build_inventory([], [orchestrator_session()]).runs[0].to_dict()

    assert payload["orchestrator"] is True, (
        "the platform's own session is indistinguishable from the runs it "
        "orchestrates in the fleet payload"
    )
    # What the mark lands beside: the row is built from the entry, so this is the
    # taskdef and target the spawn recorded.
    assert payload["taskdef"] == assistant.TASKDEF
    assert payload["target"] == assistant.TARGET


def test_the_mark_is_the_registry_kind_not_the_taskdef(tmp_path, platform_root):
    """The one way this goes quietly wrong.

    ``orchestrate`` and ``fleet`` are *arguments*: an operator can spawn a worker
    with either, and a row that inferred the role from them would badge that worker
    as the platform itself — with the exit tab's verbs beside the badge. So the mark
    reads the registry ``kind``, which only ``assistant.start`` sets, and this run
    wears the orchestrator's whole vocabulary while being a worker.
    """
    ref = plant_run(tmp_path, "impostor", state_yaml=state_yaml(
        status="in-progress", taskdef=assistant.TASKDEF, target=assistant.TARGET,
    ))
    entry = session(
        "impostor",
        task={"taskdef": assistant.TASKDEF, "target": assistant.TARGET},
    )

    run = inventory.build_inventory([ref], [entry]).runs[0]

    assert run.taskdef == assistant.TASKDEF and run.target == assistant.TARGET, (
        "the run this test is about is not the one that was planted"
    )
    assert run.orchestrator is False, (
        "a worker spawned with the orchestrator's taskdef and target is marked as "
        "the orchestrator — the mark is a name heuristic rather than the kind"
    )


def test_an_orchestrator_that_died_is_still_marked_as_the_orchestrator(platform_root):
    """The row that most needs the mark is the one saying it crashed.

    A stale entry is the crash evidence this module works from, and it carries the
    kind exactly as a live one does. Dropping the mark here would put "died
    unexpectedly" on an anonymous row at the top of the attention list.
    """
    run = inventory.build_inventory([], [orchestrator_session(live=False)]).runs[0]

    assert run.state == "crashed", "the planted entry is not the crash case"
    assert run.orchestrator is True


def test_a_run_with_no_session_claims_nothing_either_way(tmp_path, platform_root):
    """False rather than absent, and false rather than sticky.

    A client tests one field on every row, so it must always be there and always be
    a boolean — a key that appears only on one row is a key a listing forgets to
    read. And with no entry left there is no evidence either way: the mark comes
    from the registry, so a row without one says nothing rather than remembering.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="complete"))
    payload = inventory.build_inventory([ref], []).runs[0].to_dict()

    assert payload["orchestrator"] is False
    assert payload["session"] is None, "this row is supposed to have no session"


def test_the_mark_does_not_cross_through_the_session_block(platform_root):
    """Where the field crosses, stated — the same seam the title's guard pins (T57).

    ``SESSION_FIELDS`` is a narrow allowlist over a registry entry, and widening it
    with ``kind`` would ship a vocabulary the browser has no business interpreting
    in order to answer one yes/no question. So the answer is computed daemon-side
    and crosses as a field of the row.
    """
    payload = inventory.build_inventory([], [orchestrator_session()]).runs[0].to_dict()

    assert payload["orchestrator"] is True, "the mark is not a top-level field"
    assert "orchestrator" not in inventory.SESSION_FIELDS
    assert "kind" not in inventory.SESSION_FIELDS, (
        "the registry's session vocabulary crossed into the browser so a client "
        "could work the role out for itself"
    )
    assert "kind" not in payload["session"]


# --- what the run says about itself (T91) --------------------------------------
#
# ``state`` is derived, and liveness outranks the committed record there by design
# (spec D24, _derive) — which is right, and is also how a live session's row reads
# "running" while its own state.yaml says something else entirely. The row is the
# only place that can show the disagreement, so the run's own three fields cross as
# fields of the row, through the same seam the title and the orchestrator mark use.

def test_the_runs_own_account_of_itself_crosses_as_row_fields(tmp_path):
    """The committed record reaches a client, next to the derived state.

    All three, and top-level: a listing that has the derived word and nothing else
    cannot say a live session's state.yaml disagrees with it, and the disagreement
    is what makes a run look fine while it is not.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(
        status="in-progress", phase="execution", goal="build the thing",
    ))
    payload = inventory.build_inventory([ref], [session("r1")]).runs[0].to_dict()

    assert payload["status"] == "in-progress", (
        "the run's own status does not reach the payload, so no view can show what "
        "the run says about itself"
    )
    assert payload["phase"] == "execution"
    assert payload["goal"] == "build the thing"
    # The derived state is untouched by any of it: a live session is running
    # whatever the committed record says, and it is not rewritten to agree.
    assert payload["state"] == "running", (
        "the committed status was folded into the derived state; liveness outranks "
        "it deliberately (_derive), and a row that merged the two would lose the "
        "one fact this crossing exists to expose"
    )


def test_the_committed_record_does_not_cross_through_the_session_block(tmp_path, platform_root):
    """Where these fields come from, pinned — the seam the title's guard pins (T57).

    ``SESSION_FIELDS`` is a projection of a *registry entry*, and none of this is on
    one: status, phase and goal are the run's own file. Widening the allowlist to
    carry them would make the browser read a run's state out of the block it asks
    "what is running" with — and would let an entry key of the same name answer for
    the run, which is exactly backwards.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(
        status="in-progress", phase="execution", goal="build the thing",
    ))
    entry = full_entry(status="whatever the entry said", phase="entry phase")

    payload = inventory.build_inventory([ref], [entry]).runs[0].to_dict()

    assert payload["status"] == "in-progress", "the entry answered for the run"
    assert payload["phase"] == "execution"
    for field_name in ("status", "phase", "goal"):
        assert field_name not in inventory.SESSION_FIELDS
        assert field_name not in payload["session"], (
            f"{field_name} crossed inside the session block, which is a projection "
            "of the registry entry and has nothing to say about the run's own state"
        )


def test_a_run_that_has_committed_nothing_says_null_rather_than_a_word(tmp_path):
    """Bootstrapping is the common case, and it must not read as a status.

    A session in its first seconds has no run dir at all, and a run dir can exist
    before anything is written into it. Either way the keys are present — a client
    tests one field per row rather than branching on which keys arrived — and the
    value is ``None``, so a view renders nothing instead of inventing a word for
    "nobody has said".
    """
    ref = plant_run(tmp_path, "bare", state_yaml="schema: 1\n")
    payload = inventory.build_inventory([ref], []).runs[0].to_dict()

    for field_name in ("status", "phase", "goal"):
        assert field_name in payload, f"{field_name} is missing from the row entirely"
        assert payload[field_name] is None, (
            f"{field_name} reads {payload[field_name]!r} on a run that recorded "
            "nothing, so a listing would show a state the run never claimed"
        )

    # And the row built from a session alone, which is the shape a just-spawned run
    # has: there is no state.yaml to read, so there is nothing to report.
    fresh = inventory.build_inventory([], [session("brand-new")]).runs[0].to_dict()
    assert fresh["state"] == "running", "this is supposed to be the live-session row"
    assert (fresh["status"], fresh["phase"], fresh["goal"]) == (None, None, None)


# --- how long a live session has been quiet (T95) -----------------------------
#
# The gap the fleet view had: `state` is derived with liveness first (spec D24), so
# a run reads `running` from the moment its session starts until the moment it
# exits — which means a run that finished its work and is sitting at its prompt is
# indistinguishable from one that is working. The only process that knows the
# difference is the supervisor holding the PTY inside the container, so the row
# asks it while it is being built.
#
# Three things about the crossing are what these tests keep:
#
# - it goes through SESSION_FIELDS, deliberately, and inside the session block
#   rather than as a row field: it is a fact about the live session and is
#   unknowable without one;
# - it is read for live sessions that can answer, and for nothing else. This is a
#   network read on the read path of every poll, and the gates are what bound it;
# - not knowing is ordinary and reads as null — an older image, an unreachable
#   container, a session with no control plane — never as an idle of zero.

def quiet_activity(seconds=1320.0, at="2026-07-28T11:38:00Z"):
    """The record a session's control plane reports (session_io.session_activity)."""
    return {"last_output_at": at, "idle_seconds": seconds}


def live_with_control(slug, **extra):
    """A live session entry that has somewhere to be asked (a control plane)."""
    return session(slug, control={"host": "127.0.0.1", "port": 8931}, **extra)


def test_a_live_sessions_idle_reading_crosses_in_the_session_block(tmp_path):
    """The consumer story: a client can render "idle 22m" without a second request.

    Both halves cross — the number the card renders and the moment it tooltips —
    because they answer to different readers: the seconds are a measurement no
    reader's clock can spoil, the timestamp is the form that survives being written
    down.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = live_with_control("r1")

    payload = inventory.build_inventory(
        [ref], [entry], activity={"s-r1": quiet_activity()},
    ).runs[0].to_dict()

    assert payload["session"]["activity"] == quiet_activity()
    assert payload["state"] == "running", (
        "the idle reading changed the derived state; liveness decides that (D24) "
        "and a quiet session is still a running one"
    )


def test_a_session_that_reports_nothing_says_null_rather_than_idle_zero(tmp_path):
    """The mixed fleet, from the row's side.

    The supervisor that reports this ships in the session image, not in the daemon,
    so a fleet always holds sessions that will never report it — and an unreachable
    container looks the same. Both must read exactly as they did before the field
    existed. An idle of zero would say the harness had just drawn something, which
    is the opposite of what is known.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))

    payload = inventory.build_inventory(
        [ref], [live_with_control("r1")], activity={},
    ).runs[0].to_dict()

    assert payload["session"]["activity"] is None
    assert "activity" in payload["session"], (
        "the key vanished for a session that reports nothing, so the block's shape "
        "depends on whether a container answered"
    )


def test_the_control_plane_is_only_asked_about_sessions_that_could_answer(
    tmp_path, monkeypatch
):
    """What bounds the new load, which is a network read per row per poll.

    Both gates are answers the entry already contains, and both are checked before
    any I/O: a stale entry's last output is not a fact about now (and its port may
    belong to somebody else by now), and a session with no control plane cannot be
    asked anything. What is left is bounded by ``max_concurrent_sessions``.
    """
    asked = []

    def recording_activity(session_id):
        asked.append(session_id)
        return quiet_activity()

    monkeypatch.setattr(inventory, "session_activity", recording_activity)
    refs = [
        plant_run(tmp_path, "alive", state_yaml=state_yaml(status="in-progress")),
        plant_run(tmp_path, "dead", state_yaml=state_yaml(status="in-progress")),
        plant_run(tmp_path, "plain", state_yaml=state_yaml(status="in-progress")),
    ]
    entries = [
        live_with_control("alive"),
        live_with_control("dead", live=False),
        session("plain"),  # live, but never spawned with a control plane
    ]
    inv = inventory.build_inventory(refs, entries)

    assert asked == ["s-alive"], f"the wrong sessions were polled: {asked}"
    by_slug = {run.slug: run.to_dict()["session"] for run in inv.runs}
    assert by_slug["alive"]["activity"] == quiet_activity()
    assert by_slug["dead"]["activity"] is None
    assert by_slug["plain"]["activity"] is None


def test_a_caller_can_ask_for_rows_without_touching_a_container(tmp_path, monkeypatch):
    """``activity={}`` is the escape hatch, matching *titles* and *questions*.

    ``None`` means "read it" so every caller that serves a fleet view gets the fact
    without deciding anything about it; a mapping is authoritative, so ``{}`` builds
    rows with no control-plane reads at all.
    """
    asked = []
    monkeypatch.setattr(
        inventory, "session_activity", lambda sid: asked.append(sid) or None
    )
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))

    inventory.build_inventory([ref], [live_with_control("r1")], activity={})

    assert asked == [], "a caller that supplied the mapping was still polled from"


def test_a_wrong_shaped_idle_record_costs_the_field_and_not_the_row(tmp_path):
    """Same tolerance a bad ``task`` block gets: one row's metadata, never the view.

    The mapping comes from a control plane that another version of this platform
    may be talking to, so anything that is not a mapping reads as "not known".
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    for record in ("22m", 1320, ["idle"], None):
        payload = inventory.build_inventory(
            [ref], [live_with_control("r1")], activity={"s-r1": record},
        ).runs[0].to_dict()
        assert payload["state"] == "running", f"a record of {record!r} broke the row"
        assert payload["session"]["activity"] is None


def test_a_long_idle_session_is_not_put_on_the_attention_list(tmp_path):
    """The decision, recorded: idleness is a row fact, not an attention reason.

    Every member of ``ATTENTION_REASONS`` is picked up by detection automatically
    (T69) and becomes a digest class, so making one of these would mean inventing a
    threshold — how long is too long — and nothing anywhere says. The right answer
    differs by taskdef and by phase, and a wrong number spools a notification per
    session. So the fact crosses and the reader decides; the reason is one entry in
    that tuple plus one branch in ``_derive`` on the day a threshold has an owner.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    run = inventory.build_inventory(
        [ref], [live_with_control("r1")],
        activity={"s-r1": quiet_activity(seconds=86400.0)},
    ).runs[0]

    assert run.attention is None, (
        "a quiet session raised an attention record, which detection turns into a "
        "digest — on a threshold nobody has defined"
    )
    assert run.state == "running"
    assert not any("idle" in reason for reason in inventory.ATTENTION_REASONS)


def test_the_idle_reading_does_not_cross_as_a_row_field(tmp_path):
    """Where this one crosses, stated — the seam the title's guard pins the other
    side of (T57).

    The title and the orchestrator mark are row fields because they are facts about
    the *run*. This is the opposite case: it is unknowable without a live session,
    it dies with the entry, and a row-level ``idle`` would invite a client to read
    it on a dormant run and get a null it could not explain. So it goes inside the
    block the client already tests for "is something running".
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    payload = inventory.build_inventory(
        [ref], [live_with_control("r1")], activity={"s-r1": quiet_activity()},
    ).runs[0].to_dict()

    assert payload["session"]["activity"] == quiet_activity()
    for name in ("activity", "idle", "idle_seconds", "last_output_at"):
        assert name not in payload, f"{name} crossed as a row field as well"


def test_the_fold_never_writes_the_reading_onto_the_entry_it_was_given(tmp_path):
    """It changes every second, so it is folded onto a copy and never persisted.

    A registry write per live session per poll would put the fleet view's read path
    into the state directory's single writer (spec §6.1) — and the caller's entry is
    a dict the registry handed out, which nothing on a read path may mutate.
    """
    ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
    entry = live_with_control("r1")

    inventory.build_inventory([ref], [entry], activity={"s-r1": quiet_activity()})

    assert "activity" not in entry, (
        "the idle reading was written into the caller's registry entry"
    )
