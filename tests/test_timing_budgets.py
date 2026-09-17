"""Verify shared timing budgets, child diagnostics and adoption by the connector test family."""
import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests import timing_budgets as timing

REPO = Path(__file__).resolve().parents[1]

#: Modules checked for bare wall-clock bounds.
FAMILY = (
    "tests/connector_fakes.py",
    "tests/connector_probe_supervisor.py",
    "tests/test_chat_connector_notices.py",
    "tests/test_connector_delivery.py",
    "tests/test_connector_ingestion.py",
    "tests/test_connector_lifecycle.py",
    "tests/test_connector_ownership.py",
    "tests/test_connector_process.py",
    "tests/test_connector_recovery.py",
    "tests/test_connector_review_boundaries.py",
    "tests/test_lmer_cli_slack_target.py",
    "tests/test_matrix_chat.py",
    "tests/test_slack_connector.py",
    "tests/test_supervisor_teardown.py",
)

#: Also covers modules that spawn processes without constructing ChildHarness.
FORKING = (
    "tests/test_connector_ingestion.py",
    "tests/test_connector_process.py",
    "tests/test_connector_recovery.py",
    "tests/test_connector_review_boundaries.py",
    "tests/test_supervisor_teardown.py",
)

#: Excluded from FAMILY because its scan fixtures deliberately contain bare bounds.
SELF = "tests/test_timing_budgets.py"

#: Above the family's 0.005–0.05s poll intervals and below its observation windows.
INTERVAL_CEILING = 0.1

#: Also scans embedded child source, which the AST sees only as a string.
BARE_BOUND = re.compile(
    r"""(?:timeout|budget|deadline)\s*=\s*(\d*\.?\d+)      # timeout=30
      | (?:sleep|wait)\s*\(\s*(\d*\.?\d+)\s*[,)]           # sleep(60), wait(.3)
      | monotonic\(\)\s*\+\s*(\d*\.?\d+)                   # monotonic() + 30
      | DRAIN_SECONDS\s*=\s*(\d*\.?\d+)                    # a child's own drain
    """,
    re.VERBOSE,
)


#: Product-default assertions are not test wait budgets.
EXEMPT = "# product-bound"

#: Calls whose positional arguments can contain wall-clock bounds.
BOUNDED_CALLS = frozenset({
    "wait_for", "wait_until", "stop", "join", "wait", "sleep", "terminate_wait",
    "run_probe", "drain_connectors", "collect",
    # Includes monkeypatched product timing constants.
    "setattr",
})


def _bound_arguments(tree):
    """(line, description) for every literal bound written as code."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        arguments = list(node.args) if name in BOUNDED_CALLS else []
        arguments += [kw.value for kw in node.keywords
                      if kw.arg in {"timeout", "budget", "deadline", "reap"}]
        for argument in arguments:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, (int, float)):
                if not isinstance(argument.value, bool) and argument.value >= INTERVAL_CEILING:
                    yield argument.lineno, f"{name}(... {argument.value} ...)"


def _group_marks(tree):
    """Find group markers, accepting either XDIST_GROUP or its literal value."""
    marks = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "xdist_group"):
            continue
        for argument in node.args:
            named = "XDIST_GROUP" in {getattr(argument, "id", None), getattr(argument, "attr", None)}
            literal = isinstance(argument, ast.Constant) and argument.value == timing.XDIST_GROUP
            assert named or literal, ast.dump(argument)
            marks.append(node)
    return marks


def _spawning_modules():
    """Find modules that construct ChildHarness instances."""
    found = []
    for path in sorted((REPO / "tests").glob("*.py")):
        if path.name == "connector_fakes.py":
            continue
        tree = ast.parse(path.read_text())
        if any(isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ChildHarness"
               for node in ast.walk(tree)):
            found.append(str(path.relative_to(REPO)))
    return found


def _offenders(path, root=REPO):
    text = path.read_text()
    lines = text.splitlines()
    found = {}
    for number, line in enumerate(lines, 1):
        match = BARE_BOUND.search(line)
        if not match:
            continue
        value = float(next(group for group in match.groups() if group is not None))
        if value >= INTERVAL_CEILING:
            found[number] = line.strip()
    for number, description in _bound_arguments(ast.parse(text)):
        found.setdefault(number, description)
    return [f"{path.relative_to(root)}:{number}: {text}" for number, text in sorted(found.items())
            if EXEMPT not in lines[number - 1]]


class TestTheFamilyHasNoPerTestConstants:
    def test_no_bare_wall_clock_bound_in_the_family(self):
        offenders = [line for name in FAMILY for line in _offenders(REPO / name)]
        assert not offenders, (
            "wall-clock bounds must come from tests/timing_budgets.py so one "
            "factor scales them together:\n" + "\n".join(offenders)
        )

    def test_the_scan_fires_on_what_it_claims_to_catch(self, tmp_path):
        planted = tmp_path / "tests" / "planted.py"
        planted.parent.mkdir()
        planted.write_text(
            "conn.stop(timeout=30)\n"
            "worker.join(timeout=90)\n"
            "deadline = time.monotonic() + 120\n"
            "assert reached.wait(20)\n"
            "wait_for(lambda: done, child, 60)\n"
            "conn.stop(45)\n"
            "monkeypatch.setattr(supervisor, 'HANDOFF_SECONDS', 1.5)\n"
            "time.sleep(.3)  # an observation window, not an interval\n"
            'CHILD = """\nconnector_lifecycle.DRAIN_SECONDS = 60\ntime.sleep(180)\n"""\n'
            "time.sleep(0.02)  # an interval, not a bound\n"
            "mock.assert_called_with(timeout=45)  # product-bound\n"
        )
        found = _offenders(planted, root=tmp_path)
        assert len(found) == 10, found
        assert not any("0.02" in line or "assert_called_with" in line for line in found)

    @pytest.mark.parametrize("name", FORKING)
    def test_every_forking_module_is_in_the_one_group(self, name):
        assert _group_marks(ast.parse((REPO / name).read_text())), (
            f"{name} forks children but names no xdist group")

    def test_a_module_that_spawns_children_cannot_forget_the_marker(self):
        missing = [name for name in _spawning_modules()
                   if not _group_marks(ast.parse((REPO / name).read_text()))]
        assert not missing, (
            "these spawn connector children and must carry "
            f"pytest.mark.xdist_group(XDIST_GROUP): {missing}")

    def test_this_module_marks_its_own_spawning_test(self):
        marks = _group_marks(ast.parse((REPO / SELF).read_text()))
        assert marks, f"{SELF} spawns a child of its own and must name the group"

    def test_the_marker_is_declared_for_runs_without_xdist(self):
        assert "xdist_group(name)" in (REPO / "pyproject.toml").read_text()


class TestTheScaleFactor:
    @pytest.fixture(autouse=True)
    def _budget_module_restored(self):
        """Restore shared module globals so later tests use the original scale."""
        original = os.environ.get(timing.WAIT_SCALE_ENV)
        yield
        if original is None:
            os.environ.pop(timing.WAIT_SCALE_ENV, None)
        else:
            os.environ[timing.WAIT_SCALE_ENV] = original
        importlib.reload(timing)

    @pytest.mark.parametrize("value,expected", [("", 1.0), ("1", 1.0), ("4", 4.0), ("2.5", 2.5)])
    def test_accepted_values(self, monkeypatch, value, expected):
        monkeypatch.setenv(timing.WAIT_SCALE_ENV, value)
        assert importlib.reload(timing).SCALE == expected
        assert timing.SETTLE == timing.stretch(120.0)

    @pytest.mark.parametrize("value", ["0", "0.5", "-4", "four", "4x", "1e9"])
    def test_a_value_nobody_meant_is_refused_loudly(self, monkeypatch, value):
        monkeypatch.setenv(timing.WAIT_SCALE_ENV, value)
        with pytest.raises(RuntimeError, match=timing.WAIT_SCALE_ENV):
            importlib.reload(timing)

    def test_every_budget_scales_and_no_interval_does(self, monkeypatch):
        intervals = (timing.POLL_SECONDS, timing.CHILD_POLL_SECONDS)
        monkeypatch.setenv(timing.WAIT_SCALE_ENV, "4")
        scaled = importlib.reload(timing)
        assert scaled.SETTLE == 480.0 and scaled.CHILD_DRAIN == 120.0
        assert scaled.DRAIN_DEADLINE == scaled.outlast(scaled.CHILD_DRAIN_EXPIRED)
        assert (scaled.POLL_SECONDS, scaled.CHILD_POLL_SECONDS) == intervals

    def test_a_stall_outlives_the_bound_it_is_derived_from(self):
        assert timing.outlast(timing.CHILD_DRAIN_EXPIRED) > timing.CHILD_DRAIN_EXPIRED
        assert timing.DRAIN_DEADLINE > timing.CHILD_DRAIN_EXPIRED

    def test_the_orderings_the_family_relies_on(self):
        """Inverted budget relations make tests time out instead of reaching their assertions."""
        assert timing.PROBE > timing.SETTLE
        assert timing.PROBE_KILL < timing.PROBE and timing.PROBE_KILL < timing.SETTLE
        # The held-open drain is derived in test_connector_review_boundaries.py.
        assert timing.SHUTDOWN < timing.FIXTURE_RELEASE < timing.outlast(timing.FIXTURE_RELEASE)
        assert timing.STOP > timing.CHILD_DRAIN
        # The product's drain default is 45s.
        assert timing.DRAIN > 45.0 * timing.SCALE
        assert timing.CHILD_DRAIN_EXPIRED < timing.DRAIN_DEADLINE < timing.SHUTDOWN
        assert timing.CHILD_ALIVE > timing.TEARDOWN_BOUND > timing.HANDOFF
        assert timing.LATE_HANDOFF > timing.TEARDOWN_BOUND
        assert timing.outlast(timing.BUSY_BOUND) > timing.BUSY_BOUND

    def test_the_suite_harness_keeps_the_factor_out_of_the_stripped_set(self):
        from tests import conftest

        assert conftest.WAIT_SCALE_ENV == timing.WAIT_SCALE_ENV
        assert timing.WAIT_SCALE_ENV in conftest._HARNESS_ENV

    def test_the_gate_runner_and_the_suite_name_the_same_variable(self):
        source = (REPO / "ci/gate-tests.py").read_text()
        assert f'WAIT_SCALE_ENV = "{timing.WAIT_SCALE_ENV}"' in source
        assert '"--dist", "loadgroup"' in source

    def test_the_variable_reaches_the_container(self):
        source = (REPO / "src/lmer_cli/cli.py").read_text()
        assert f'"{timing.WAIT_SCALE_ENV}": os.environ.get("{timing.WAIT_SCALE_ENV}")' in source
        assert timing.WAIT_SCALE_ENV in (REPO / "docs/LMER-CLI.md").read_text()

    def test_running_the_family_serially_is_documented(self):
        doc = (REPO / "docs/GATE-FASTPATH.md").read_text()
        for text in (timing.WAIT_SCALE_ENV, timing.XDIST_GROUP, "--dist loadgroup"):
            assert text in doc


class TestTheEvidenceAChildLeaves:
    def test_a_child_that_never_logged_still_names_its_state(self, tmp_path):
        empty = tmp_path / "child.log"
        empty.write_text("")
        harness = type("Harness", (), {})()
        harness.children = [subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])]
        harness.children[0].wait()
        harness.log_paths = [empty, tmp_path / "never.log"]
        evidence = timing.child_evidence(harness)
        assert "exited 7" in evidence
        assert f"{empty}: empty" in evidence
        assert "never.log: never written" in evidence

    def test_a_child_log_is_quoted_whole(self, tmp_path):
        log = tmp_path / "child.log"
        log.write_text("[fixture-child 1] start argv=['slack']\ntraceback here\n")
        harness = type("Harness", (), {"children": [], "log_paths": [log]})()
        assert "traceback here" in timing.child_evidence(harness)

    def test_the_fixture_child_says_what_it_is_doing(self):
        from tests.connector_fakes import CHILD_SOURCE

        for text in ("faulthandler.enable()", "logging.basicConfig", 'say("start argv',
                     'say("exit code', 'say("reload exec"'):
            assert text in CHILD_SOURCE

    @pytest.mark.xdist_group(timing.XDIST_GROUP)
    def test_a_fixture_child_writes_its_lines_to_the_log_it_is_given(self, tmp_path):
        from lmer_cli.connector_process import spawn_child

        script = tmp_path / "child.py"
        script.write_text(
            "import sys\n"
            "print('on stdout')\n"
            "print('on stderr', file=sys.stderr)\n"
        )
        log = tmp_path / "child.log"
        process = spawn_child([sys.executable, str(script)], {**os.environ, "PYTHONUNBUFFERED": "1"}, log)
        assert process.wait(timeout=timing.CLI) == 0
        assert "on stdout" in log.read_text() and "on stderr" in log.read_text()

    def test_the_children_are_told_the_same_budgets(self):
        bounds = timing.child_bounds()
        assert float(bounds["CONNECTOR_FIXTURE_DRAIN"]) == timing.CHILD_DRAIN
        assert float(bounds["CONNECTOR_FIXTURE_SETTLE"]) == timing.SETTLE
        assert float(bounds["LMER_COMMS_CONNECT_BUDGET"]) == timing.CHILD_CONNECT
        # Children consume resolved budgets, not the scale factor.
        assert timing.WAIT_SCALE_ENV not in bounds
