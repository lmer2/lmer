"""The container handshake for a pre-minted execution uid (spec §6.2, D7).

The host mints a uid and passes ``--launch-uid``; the container must record
EXACTLY that uid, and must refuse a uid that already names a record rather than
adopting it. ``_record_address`` resolves by launch uid first, so the refusal is
the only thing standing between a reused uid and two executions writing one
record.
"""

import os
from pathlib import Path

import pytest

from tests.conftest import strip_lmer_env
from tests.test_schema_v2_sessions import (  # noqa: F401
    session_tree, seed_launch, _run_container_session_start, _snapshot_record,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _container_env(session_tree, **overrides):
    env = dict(os.environ)
    env.update({
        "LMER_WORK_REPO": "https://git.example.test/agents/work",
        "LMER_WORK_REPO_PATH": str(session_tree),
        "LMER_REPO_HOST": "git.example.test",
        "LMER_REPO_PROJECT": "acme/widgets",
        "LMER_REPO_URL": "https://git.example.test/acme/widgets",
        "LMER_TASK": "chat",
        "LMER_TASK_TARGET": "",
    })
    env.update(overrides)
    return env


def _run_area_snapshot(session_tree):
    return {
        item.relative_to(session_tree).as_posix(): item.read_bytes()
        for item in session_tree.rglob("*")
        if item.is_file()
    }


def test_a_reused_launch_uid_is_refused_and_writes_nothing(session_tree, tmp_path):
    metadata, record, _state = seed_launch()
    before = _run_area_snapshot(session_tree)

    captured = _run_container_session_start(
        _container_env(session_tree, LMER_LAUNCH_UID=metadata["uid"]),
        tmp_path / "markers",
    )

    assert captured["main_rc"] == 2
    assert _run_area_snapshot(session_tree) == before, (
        "a refused launch must leave the work tree byte-identical"
    )


def test_a_fresh_launch_uid_is_the_record_it_opens(session_tree, tmp_path):
    uid = "f" * 32

    captured = _run_container_session_start(
        _container_env(session_tree, LMER_LAUNCH_UID=uid),
        tmp_path / "markers",
    )

    assert captured["main_rc"] == 0
    assert captured["writer_uid"] == uid
    assert captured["record"].name == uid
    assert Path(captured["record"]).is_dir()


def test_start_launch_consumes_the_launch_uid_once(session_tree, monkeypatch):
    from work_repo import session_record

    uid = "e" * 32
    monkeypatch.setenv("LMER_LAUNCH_UID", uid)

    metadata = session_record.start_launch()

    assert metadata["uid"] == uid
    assert "LMER_LAUNCH_UID" not in os.environ, (
        "the uid is one-shot: a child session must mint its own"
    )
    assert session_record.start_launch()["uid"] != uid


def test_a_malformed_launch_uid_is_ignored_and_a_fresh_one_minted(
    session_tree, monkeypatch
):
    from work_repo import session_record

    monkeypatch.setenv("LMER_LAUNCH_UID", "not-a-uid")

    metadata = session_record.start_launch()

    assert metadata["uid"] != "not-a-uid"
    assert len(metadata["uid"]) == 32
    assert "LMER_LAUNCH_UID" not in os.environ


# --- an unreadable run area refuses the launch (review !387, discussion 14) ---
#
# "No record names this uid" is only an answer when the run area was read: a
# base that exists and cannot be listed must refuse the launch — main() turns
# the raise into exit 2 — rather than read as empty. A launch with no run area
# at all (repo-less) has nothing a reused uid could adopt.


def test_a_launch_with_no_run_area_holds_no_record(monkeypatch):
    from lmer_cli.container import clone_and_exec
    from work_repo import run_state

    monkeypatch.setattr(run_state, "runs_base", lambda: None)

    assert clone_and_exec._launch_uid_names_a_record("f" * 32) is False


def test_a_launch_uid_changes_nothing_for_a_no_repo_launch(
    session_tree, tmp_path, capfd
):
    """The daemon passes --launch-uid to every schema-2 spawn, repo-less ones too:
    the uid check must not be what decides such a launch."""
    def launch(name, **extra):
        env = _container_env(session_tree, LMER_NO_REPO="1", **extra)
        for part in ("LMER_REPO_HOST", "LMER_REPO_PROJECT", "LMER_REPO_URL"):
            env.pop(part, None)
        rc = _run_container_session_start(env, tmp_path / name)["main_rc"]
        return rc, capfd.readouterr().err

    without_rc, _ = launch("plain")
    with_rc, err = launch("uid", LMER_LAUNCH_UID="e" * 32)

    assert with_rc == without_rc
    assert "launch uid" not in err and "run area" not in err


def test_a_run_area_that_cannot_be_listed_refuses_the_launch(monkeypatch, tmp_path):
    from lmer_cli.container import clone_and_exec
    from work_repo import run_state

    unlistable = tmp_path / "_runs"
    unlistable.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(run_state, "runs_base", lambda: unlistable)

    with pytest.raises(RuntimeError, match="cannot be listed"):
        clone_and_exec._launch_uid_names_a_record("f" * 32)


def test_a_run_area_not_created_yet_holds_no_record(monkeypatch, tmp_path):
    from lmer_cli.container import clone_and_exec
    from work_repo import run_state

    monkeypatch.setattr(run_state, "runs_base", lambda: tmp_path / "_runs")

    assert clone_and_exec._launch_uid_names_a_record("f" * 32) is False


def test_a_listed_run_area_finds_the_record_a_uid_names(monkeypatch, tmp_path):
    from lmer_cli.container import clone_and_exec
    from work_repo import run_state

    uid = "d" * 32
    record = tmp_path / "_runs" / "chat" / uid
    record.mkdir(parents=True)
    (record / "state.yaml").write_text("schema: 2\nslug: chat\n", encoding="utf-8")
    monkeypatch.setattr(run_state, "runs_base", lambda: tmp_path / "_runs")

    assert clone_and_exec._launch_uid_names_a_record(uid) is True
    assert clone_and_exec._launch_uid_names_a_record("c" * 32) is False
