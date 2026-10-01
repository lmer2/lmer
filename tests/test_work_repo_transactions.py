"""The checkout lock is keyed on the tree being written (work_repo.transactions).

The writers it wraps take the tree they mutate as an argument. Locking on the
process environment instead would take the configured checkout's lock for a
write that never touches it: a suite that strips ``LMER_*`` and writes to
``tmp_path`` would then hold the operational ``/work`` lock for the duration
of every write, and two writers of one foreign tree would not serialize at all.
"""
import threading
from pathlib import Path

import pytest

from tests.conftest import strip_lmer_env
from tests.timing_budgets import JOIN, QUIET
from work_repo import run_state
from work_repo.loggers import YamlFileLogger
from work_repo.transactions import checkout_lock, checkout_of, work_mutation


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """A configured checkout (has a manifest) beside a foreign tree."""
    root = tmp_path / "configured"
    root.mkdir()
    (root / "_plexus.yaml").write_text("schema: 1\nname: example\n")
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    return root


def _write_in_thread(write):
    done = threading.Event()
    errors = []

    def run():
        try:
            write()
        except BaseException as exc:  # pragma: no cover - a failure prints why
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run, name="writer")
    thread.start()
    return thread, done, errors


class TestCheckoutOf:
    def test_a_path_inside_the_configured_checkout_takes_its_lock(self, configured):
        rdir = configured / "widgets" / "_runs" / "chat" / "r"
        assert checkout_of(rdir) == configured.resolve()
        assert checkout_of(configured) == configured.resolve()

    def test_a_foreign_tree_with_its_own_checkout_takes_that_one(self, configured, tmp_path):
        other = tmp_path / "other"
        (other / "_runs").mkdir(parents=True)
        (other / "_plexus.yaml").write_text("schema: 1\nname: other\n")
        assert checkout_of(other / "_runs" / "r") == other.resolve()
        git_tree = tmp_path / "clone"
        (git_tree / ".git").mkdir(parents=True)
        assert checkout_of(git_tree / "deep" / "r") == git_tree.resolve()

    def test_a_tree_belonging_to_no_checkout_takes_none(self, configured, tmp_path):
        assert checkout_of(tmp_path / "scratch" / "r") is None

    def test_the_default_checkout_is_the_process_environment(self, monkeypatch):
        monkeypatch.delenv("LMER_WORK_REPO_PATH", raising=False)
        assert checkout_of(Path("/work/acme/_runs/r")) == Path("/work")


class TestTheLockFollowsTheTree:
    def test_a_write_outside_the_configured_checkout_does_not_wait_on_its_lock(
        self, configured, tmp_path
    ):
        rdir = tmp_path / "scratch" / "r"
        with checkout_lock(configured):
            thread, done, errors = _write_in_thread(
                lambda: run_state.write_state(rdir, {"schema": 1, "slug": "foreign"})
            )
            assert done.wait(JOIN), "a foreign-tree write blocked on the configured checkout's lock"
        thread.join(JOIN)
        assert not errors, errors
        assert run_state.load_state(rdir)["slug"] == "foreign"

    def test_a_write_inside_the_configured_checkout_waits_for_its_lock(self, configured):
        rdir = configured / "widgets" / "_runs" / "chat" / "r"
        with checkout_lock(configured):
            thread, done, errors = _write_in_thread(
                lambda: run_state.write_state(rdir, {"schema": 1, "slug": "inside"})
            )
            assert not done.wait(QUIET), "the write ran while the checkout was locked"
            assert not (rdir / run_state.STATE_FILE).exists()
        assert done.wait(JOIN)
        thread.join(JOIN)
        assert not errors, errors
        assert run_state.load_state(rdir)["slug"] == "inside"

    def test_a_foreign_checkout_serializes_on_itself(self, configured, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        (other / "_plexus.yaml").write_text("schema: 1\nname: other\n")
        rdir = other / "widgets" / "_runs" / "chat" / "r"
        with checkout_lock(other):
            thread, done, errors = _write_in_thread(
                lambda: run_state.write_ledger(rdir, {"schema": run_state.LEDGER_SCHEMA_VERSION, "tasks": {}})
            )
            assert not done.wait(QUIET), "the write ran while its own checkout was locked"
        assert done.wait(JOIN)
        thread.join(JOIN)
        assert not errors, errors
        assert run_state.load_ledger(rdir)["tasks"] == {}

    def test_the_logger_is_keyed_on_its_log_file(self, configured, tmp_path):
        log_file = tmp_path / "scratch" / "log.yaml"
        with checkout_lock(configured):
            thread, done, errors = _write_in_thread(
                lambda: YamlFileLogger(log_file).log("outside the configured checkout")
            )
            assert done.wait(JOIN), "a foreign log write blocked on the configured checkout's lock"
        thread.join(JOIN)
        assert not errors, errors
        assert "outside the configured checkout" in log_file.read_text()
        inside = configured / "widgets" / "_runs" / "chat" / "r" / "log.yaml"
        with checkout_lock(configured):
            thread, done, errors = _write_in_thread(lambda: YamlFileLogger(inside).log("inside"))
            assert not done.wait(QUIET), "a log write inside the checkout ran while it was locked"
        assert done.wait(JOIN)
        thread.join(JOIN)
        assert not errors, errors

    def test_work_mutation_without_a_tree_still_locks_the_configured_checkout(self, configured):
        entered = threading.Event()

        def enter():
            with work_mutation():
                entered.set()

        with checkout_lock(configured):
            thread, done, errors = _write_in_thread(enter)
            assert not entered.wait(QUIET)
        assert done.wait(JOIN)
        thread.join(JOIN)
        assert not errors, errors
