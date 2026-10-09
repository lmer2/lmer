"""Schema-2 launches own fresh records; context never grants write ownership."""

import json
import os
import re
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from lmer_schema.manifest import ManifestError, parse_manifest
from tests.conftest import strip_lmer_env
from work_repo import run_state, utils


@pytest.fixture
def session_tree(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    root = tmp_path / "plexus"
    root.mkdir()
    (root / "_plexus.yaml").write_text(
        "schema: 2\nname: example\nprojects:\n  widgets:\n"
        "    repo: git.example.test/acme/widgets\n"
    )
    for key, value in {
        "LMER_WORK_REPO_PATH": str(root),
        "LMER_REPO_HOST": "git.example.test",
        "LMER_REPO_PROJECT": "acme/widgets",
        "LMER_REPO_URL": "https://git.example.test/acme/widgets",
        "LMER_TASK": "chat",
        "LMER_TASK_TARGET": "",
    }.items():
        monkeypatch.setenv(key, value)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    return root


def test_schema_two_is_readable_and_newer_schema_refuses():
    assert parse_manifest("schema: 2\nname: example\n").schema == 2
    with pytest.raises(ManifestError, match="upgrade lmer"):
        parse_manifest("schema: 3\nname: example\n")


def test_host_launch_metadata_uses_one_precedence_policy(monkeypatch):
    from work_repo import session_record

    monkeypatch.setattr(session_record.getpass, "getuser", lambda: "launcher-user")
    monkeypatch.setattr(
        session_record.socket, "gethostname", lambda: "launcher.example"
    )
    monkeypatch.setattr(session_record.os, "getpid", lambda: 4321)
    supplied = {
        "operator": "origin-user",
        "hostname": "origin.example",
        "model": "predecessor-model",
        "effort": "predecessor-effort",
        "config": {
            "future": "inherited",
            "api_token": "private-value",
        },
        "extension": {"answer": [1, 2]},
        "launcher": {
            "extension": "survives",
            "config": {"launcher_future": True},
        },
    }

    metadata = session_record.compose_launch_metadata(
        supplied,
        harness="codex",
        model="current-model",
        effort=None,
        config={"preset": None, "future": "resolved"},
    )

    assert metadata["operator"] == "origin-user"
    assert metadata["hostname"] == "origin.example"
    assert metadata["harness"] == "codex"
    assert metadata["model"] == "current-model"
    assert metadata["effort"] is None
    assert metadata["config"] == {
        "future": "resolved",
        "api_token": "***REDACTED***",
        "preset": None,
    }
    assert metadata["extension"] == {"answer": [1, 2]}
    assert metadata["launcher"]["extension"] == "survives"
    assert metadata["launcher"]["operator"] == "launcher-user"
    assert metadata["launcher"]["hostname"] == "launcher.example"
    assert metadata["launcher"]["pid"] == 4321
    assert metadata["launcher"]["model"] == "current-model"
    assert metadata["launcher"]["effort"] is None
    assert metadata["launcher"]["config"] == {
        "launcher_future": True,
        "future": "resolved",
        "api_token": "***REDACTED***",
        "preset": None,
    }
    assert supplied["model"] == "predecessor-model"
    assert supplied["config"]["api_token"] == "private-value"


def test_new_launch_uid_does_not_encode_provenance(session_tree, monkeypatch):
    from work_repo import session_record

    monkeypatch.setenv("LMER_SESSION_ID", "a" * 32)
    monkeypatch.setenv("LMER_SESSION_METADATA", json.dumps({
        "operator": "alice", "hostname": "build.example", "harness": "future",
        "model": "custom-model", "effort": "custom-effort",
        "config": {"temperature": 0.3}, "extension": {"answer": [1, 2]},
    }))
    first = session_record.start_launch()
    second = session_record.start_launch()
    assert re.fullmatch(r"[0-9a-f]{32}", first["uid"])
    assert first["uid"] != second["uid"] != "a" * 32
    assert first["operator"] == "alice"
    assert first["hostname"] == "build.example"
    assert first["execution_hostname"] == socket.gethostname()
    assert first["pid"] > 0 and first["started_at"]
    assert first["harness"] == "future"
    assert first["model"] == "custom-model"
    assert first["effort"] == "custom-effort"
    assert first["config"] == {"temperature": 0.3}
    assert first["extension"] == {"answer": [1, 2]}


def test_freeform_config_preserves_unknowns_but_redacts_secrets(session_tree, monkeypatch):
    from work_repo import session_record

    monkeypatch.setenv("LMER_SESSION_METADATA", json.dumps({
        "config": {"api_token": "private-value", "experimental": ["new", {"x": 7}]},
        "custom_field": "use https://user:password@git.example.test/acme/widgets",
    }))
    record = session_record.start_launch()
    assert record["config"]["experimental"] == ["new", {"x": 7}]
    assert "private-value" not in json.dumps(record)
    assert "user:password" not in json.dumps(record)


def seed_launch():
    from work_repo import session_record

    metadata = session_record.start_launch()
    rdir, state = run_state.ensure_run()
    return metadata, rdir, state


def test_fresh_record_retains_metadata_and_unknown_fields(session_tree, monkeypatch):
    monkeypatch.setenv("LMER_SESSION_METADATA", '{"config":{"future":{"x":3}},"custom":true}')
    metadata, rdir, state = seed_launch()
    assert rdir.name == metadata["uid"]
    assert state["schema"] == 2
    assert state["session"]["custom"] is True
    assert state["session"]["config"] == {"future": {"x": 3}}
    state["session"]["later_extension"] = [4, 5]
    run_state.write_state(rdir, state)
    assert run_state.load_state(rdir)["session"]["later_extension"] == [4, 5]


def test_default_label_derivation_and_shape():
    """D5: `<taskdef>-<yymmdd>-<uid[:4]>`, the date in UTC; the shape test
    accepts an inherited default label for the record's own taskdef."""
    uid = "abcd" * 8
    assert run_state.derive_default_label(
        "chat", "2026-10-07T23:30:00-02:00", uid
    ) == "chat-261008-abcd"
    state = {
        "taskdef": "chat",
        "name": "chat-261008-abcd",
        "session": {"uid": uid, "started_at": "2026-10-07T23:30:00-02:00"},
    }
    assert run_state.is_default_label(state)
    state["name"] = "chat-261007-3f9a"  # an inherited default label
    assert run_state.is_default_label(state)
    state["name"] = "tidy"
    assert not run_state.is_default_label(state)
    assert not run_state.is_default_label({"name": "x"})


def test_a_freshly_seeded_record_carries_a_default_label_without_run_named(
    session_tree, monkeypatch
):
    metadata, rdir, state = seed_launch()
    assert state["name"] == run_state.derive_default_label(
        "chat", state["session"]["started_at"], metadata["uid"]
    )
    assert run_state.is_default_label(state)
    assert "run_named" not in [e["type"] for e in run_state.read_events(rdir, last_n=0)]


def test_freeze_does_not_rename_a_default_labelled_record(session_tree):
    _, rdir, state = seed_launch()
    assert run_state.is_default_label(state)
    frozen, old_name = run_state.freeze_run_dir(rdir, state)
    assert frozen == rdir and old_name is None
    assert state["frozen"] is not None


def test_launch_name_replaces_a_default_label_on_the_opening_call(
    session_tree, monkeypatch
):
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_RUN_NAME", "tidy-index")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    state = run_state.load_state(run_state.run_dir())
    assert state["name"] == "tidy-index"


def _continue_from(source_state, monkeypatch):
    from work_repo import session_record

    session_record.start_launch()
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", source_state["session"]["uid"])
    return run_state.ensure_run()


def test_continuation_inherits_the_source_name(session_tree, monkeypatch):
    _, source, source_state = seed_launch()
    source_state["name"] = "tidy"
    run_state.write_state(source, source_state)
    run_state.append_event(source, "session_end")
    _, state = _continue_from(source_state, monkeypatch)
    assert state["name"] == "tidy"


def test_continuation_inherits_a_default_label_and_stays_unnamed_for_the_freeze(
    session_tree, monkeypatch
):
    from work_repo import specs_index

    _, source, source_state = seed_launch()
    assert run_state.is_default_label(source_state)
    run_state.append_event(source, "session_end")
    target, state = _continue_from(source_state, monkeypatch)
    assert state["name"] == source_state["name"]
    assert run_state.is_default_label(state)
    frozen, old_name = run_state.freeze_run_dir(target, state)
    assert frozen == target and old_name is None
    assert specs_index.run_label(target, state).startswith(state["slug"])


def test_launch_name_replaces_an_inherited_name_on_the_opening_call(
    session_tree, monkeypatch
):
    from work_repo import cli

    _, source, source_state = seed_launch()
    source_state["name"] = "tidy"
    run_state.write_state(source, source_state)
    run_state.append_event(source, "session_end")
    target, state = _continue_from(source_state, monkeypatch)
    assert state["name"] == "tidy"
    monkeypatch.setenv("LMER_RUN_NAME", "tidy-v2")
    assert cli.cmd_session_start() == 0
    state = run_state.load_state(target)
    assert state["name"] == "tidy-v2"
    events = run_state.read_events(target, last_n=0)
    assert any(
        e["type"] == "run_named" and (e.get("data") or {}).get("source") == "launch"
        for e in events
    )


def test_goals_freeze_leaves_a_default_labelled_record_unnamed(session_tree, capsys):
    from work_repo import cli

    _, rdir, state = seed_launch()
    assert run_state.is_default_label(state)
    (rdir / "goals.md").write_text(
        "## G1: ship it\nsignal: test\nevidence: tests/test_x.py\n"
    )
    assert cli._cmd_goals_freeze(None) == 0
    assert "not named yet" in capsys.readouterr().out
    assert run_state.load_state(rdir)["frozen"] is None
    assert run_state.run_dir() == rdir


def test_specs_index_label_uses_the_slug_for_a_default_label(session_tree):
    from work_repo import specs_index

    _, rdir, state = seed_launch()
    label = specs_index.run_label(rdir, state)
    assert label.startswith(state["slug"])
    assert run_state.is_default_label(state)
    state["name"] = "tidy-index"
    assert specs_index.run_label(rdir, state).startswith("tidy-index")


def test_continuation_uses_fresh_record_and_preserves_source_bytes(session_tree, monkeypatch):
    old, source, source_state = seed_launch()
    source_state["goal"] = "Finish the existing work"
    run_state.write_state(source, source_state)
    run_state.append_event(source, "session_end")
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    from work_repo import session_record

    fresh = session_record.start_launch()
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    target, state = run_state.ensure_run()
    assert target != source
    assert target.name == fresh["uid"]
    assert state["session"]["source_session_uid"] == old["uid"]
    assert state["context"]["state"]["goal"] == "Finish the existing work"
    assert state["owner"] is None
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == before


def test_unsigned_source_requires_explicit_source_bound_decision(session_tree, monkeypatch):
    old, source, _ = seed_launch()
    from work_repo import session_record

    new = session_record.start_launch()
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    with pytest.raises(run_state.RunStateError, match="sign.out"):
        run_state.ensure_run()
    assert not (source.parent / new["uid"]).exists()
    monkeypatch.setenv("LMER_ALLOW_UNSIGNED_SOURCE", "some-other-session")
    with pytest.raises(run_state.RunStateError, match="sign.out"):
        run_state.ensure_run()
    monkeypatch.setenv("LMER_ALLOW_UNSIGNED_SOURCE", old["uid"])
    target, state = run_state.ensure_run()
    assert target.name == new["uid"]
    assert state["session"]["unsigned_source_acknowledged"] == old["uid"]


def test_another_launch_cannot_write_source_record(session_tree):
    _, source, state = seed_launch()
    before = (source / "state.yaml").read_bytes()
    from work_repo import session_record

    session_record.start_launch()
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.write_state(source, state)
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.append_event(source, "session_start")
    assert (source / "state.yaml").read_bytes() == before


def test_uid_sorting_uses_logged_time_not_random_lexical_order(session_tree):
    group = session_tree / "widgets" / "_runs" / "chat"
    for uid, created in [("f" * 32, "2026-09-16T00:00:00Z"), ("0" * 32, "2026-09-17T00:00:00Z")]:
        directory = group / uid
        directory.mkdir(parents=True)
        (directory / "state.yaml").write_text(yaml.safe_dump({
            "schema": 2, "created": created, "session": {"uid": uid, "started_at": created},
        }))
    os.utime(group / ("f" * 32), (2000000000, 2000000000))
    assert run_state._newest_record(list(group.iterdir())).name == "0" * 32


def test_session_start_logs_full_freeform_metadata(session_tree, monkeypatch, capsys):
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_SESSION_METADATA", '{"harness":"future","custom":{"x":1}}')
    metadata = session_record.start_launch()
    assert cli.cmd_session_start() == 0
    directory = run_state.run_dir()
    starts = [e for e in run_state.read_events(directory, last_n=0) if e["type"] == "session_start"]
    assert starts[-1]["data"]["session"]["uid"] == metadata["uid"]
    assert starts[-1]["data"]["session"]["custom"] == {"x": 1}
    assert starts[-1]["data"]["session"]["pid"] == metadata["pid"]


def test_cannot_relabel_old_record_to_steal_ownership(session_tree):
    _, source, state = seed_launch()
    from work_repo import session_record

    state["session"] = session_record.start_launch()
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.write_state(source, state)


def test_future_record_version_refuses_every_writer(session_tree):
    _, directory, old_state = seed_launch()
    path = directory / "state.yaml"
    future_state = {**old_state, "schema": run_state.SCHEMA_VERSION + 1}
    path.write_text(yaml.safe_dump(future_state))
    before = path.read_bytes()
    with pytest.raises(run_state.RunStateError, match="newer"):
        run_state.append_event(directory, "session_end")
    with pytest.raises(run_state.RunStateError, match="newer"):
        run_state.write_state(directory, old_state)
    assert path.read_bytes() == before

def _set_git_identity(monkeypatch):
    """Give the work-repo commits an explicit identity (CI has no gitconfig)."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "test@example.com")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=test",
         "-c", "user.email=test@example.com", *args],
        check=True, capture_output=True,
    )


def _git_work_repo(session_tree):
    """A bare origin plus a clone holding the manifest, as LMER_WORK_REPO_PATH."""
    origin = session_tree.parent / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    clone = session_tree.parent / "workrepo"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    (clone / "_plexus.yaml").write_text((session_tree / "_plexus.yaml").read_text())
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "init")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return clone, origin


def _tracked(clone):
    _git(clone, "fetch", "-q", "origin")
    return subprocess.run(
        ["git", "-C", str(clone), "ls-tree", "-r", "--name-only", "origin/main"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_work_commit_publishes_the_seeded_record(session_tree, monkeypatch):
    """`work seed` into another group, then `work commit`, publishes both the
    launch's own record and the seed (#535, spec §5.2)."""
    from types import SimpleNamespace

    from work_repo.cli import cmd_seed
    from work_repo.git_ops import commit_work_changes

    clone, _ = _git_work_repo(session_tree)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(clone))
    _set_git_identity(monkeypatch)
    _, own_rdir, _ = seed_launch()
    rc = cmd_seed(SimpleNamespace(taskdef="develop", target="other-target", name=None, goal=None))
    assert rc == 0
    seeded = run_state.seeded_record_rel_paths()
    assert len(seeded) == 1
    assert commit_work_changes("publish the seed") == 0
    tracked = _tracked(clone)
    assert own_rdir.relative_to(clone).as_posix() in tracked
    assert seeded[0] in tracked
    # Nothing outside the two records is left behind.
    assert subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == ""


def test_a_removed_seeded_group_stages_nothing(session_tree, monkeypatch):
    """A seed whose group was removed before the commit stages nothing and
    does not fail the commit (#535)."""
    import shutil

    from work_repo.git_ops import commit_work_changes
    from work_repo.session_record import seeded_session

    clone, _ = _git_work_repo(session_tree)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(clone))
    _set_git_identity(monkeypatch)
    seed_launch()
    # Seed directly, then remove the group before committing.
    rdir, _ = run_state.seed_run_dir(
        "develop-other-target", "develop", "other-target",
        adopt_existing=False, seeded=seeded_session(),
    )
    run_state.note_seeded_record(rdir)
    shutil.rmtree(rdir.parent)
    assert commit_work_changes("seed gone") == 0


def _seeded_state(uid, seeded_by):
    """The record `work seed` writes under D13: a fresh uid, the seeding
    launch in `seeded_by`, nothing run in it yet."""
    state = run_state.seed_state("develop-other-target", "develop", "other-target")
    state["session"] = {"uid": uid, "seeded_by": seeded_by, "started_at": state["created"]}
    return state


def _plant(directory, state):
    directory.mkdir(parents=True)
    (directory / "state.yaml").write_text(yaml.safe_dump(state, sort_keys=False))
    return _snapshot_record(directory)


def _assert_read_only(directory, state, before):
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.write_state(directory, state)
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.append_event(directory, "goal_set")
    assert _snapshot_record(directory) == before


def test_seeding_launch_writes_its_seed_while_it_is_a_temp_dir(session_tree):
    metadata, _, _ = seed_launch()
    seeded_uid = "b" * 32
    tmp = session_tree / "widgets" / "_runs" / f".new-{seeded_uid}-tmp"
    tmp.mkdir()
    state = _seeded_state(seeded_uid, metadata["uid"])
    run_state.write_state(tmp, state)  # first write: nothing on disk yet
    run_state.append_event(tmp, "run_seeded")  # ownership now read from disk
    state["goal"] = "Seeded for a later launch"
    run_state.write_state(tmp, state)
    persisted = run_state.load_state(tmp)
    assert persisted["session"] == {
        "uid": seeded_uid, "seeded_by": metadata["uid"], "started_at": state["created"],
    }
    assert persisted["goal"] == "Seeded for a later launch"
    assert [e["type"] for e in run_state.read_events(tmp, last_n=0)] == ["run_seeded"]


def test_another_launch_cannot_write_the_seed_temp_dir(session_tree):
    seeder, _, _ = seed_launch()
    seeded_uid = "b" * 32
    tmp = session_tree / "widgets" / "_runs" / f".new-{seeded_uid}-tmp"
    state = _seeded_state(seeded_uid, seeder["uid"])
    before = _plant(tmp, state)
    from work_repo import session_record

    session_record.start_launch()
    _assert_read_only(tmp, state, before)


def test_in_memory_seeded_by_cannot_override_the_persisted_seeder(session_tree):
    metadata, _, _ = seed_launch()
    seeded_uid = "b" * 32
    tmp = session_tree / "widgets" / "_runs" / f".new-{seeded_uid}-tmp"
    before = _plant(tmp, _seeded_state(seeded_uid, "c" * 32))
    _assert_read_only(tmp, _seeded_state(seeded_uid, metadata["uid"]), before)


@pytest.mark.parametrize("uid, dir_name", [
    ("b" * 32, f".new-{'c' * 32}-tmp"),
    ("not-a-uid", ".new-not-a-uid-tmp"),
], ids=["other-uid-prefix", "non-uid"])
def test_seed_exception_needs_the_records_own_uid_prefix(session_tree, uid, dir_name):
    metadata, _, _ = seed_launch()
    directory = session_tree / "widgets" / "_runs" / dir_name
    state = _seeded_state(uid, metadata["uid"])
    before = _plant(directory, state)
    _assert_read_only(directory, state, before)


def test_seeded_record_at_its_final_address_is_read_only_to_the_seeder(session_tree):
    metadata, _, _ = seed_launch()
    seeded_uid = "b" * 32
    group = session_tree / "widgets" / "_runs" / "develop-other-target"
    state = _seeded_state(seeded_uid, metadata["uid"])
    before = _plant(group / seeded_uid, state)
    _assert_read_only(group / seeded_uid, state, before)
    # Nor does the very first write get it anywhere but the temp dir.
    empty = group / ("d" * 32)
    empty.mkdir()
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.write_state(empty, _seeded_state("d" * 32, metadata["uid"]))
    assert list(empty.iterdir()) == []


def test_the_writer_refusal_names_the_continuation_route(session_tree):
    """D14: a schema-2 record another launch owns is continued as a new
    execution, and the refusal names that route with the record's values."""
    from work_repo import session_record

    seeder, _, _ = seed_launch()
    uid = "e" * 32
    group = session_tree / "widgets" / "_runs" / "develop-other-target"
    state = _seeded_state(uid, seeder["uid"])
    _plant(group / uid, state)
    session_record.start_launch()  # a different launch, owning no such record
    with pytest.raises(run_state.RunStateError, match=f"--source-session {uid}"):
        run_state.write_state(group / uid, state)


def test_two_continuations_carry_one_label_and_nothing_refuses(
    session_tree, monkeypatch, capsys
):
    """D4: two continuations of one source become two records carrying one
    label; the legacy uniqueness gate never refuses on schema 2."""
    from work_repo import cli, session_record

    _, source, source_state = seed_launch()
    source_state["name"] = "carried"
    run_state.write_state(source, source_state)
    run_state.append_event(source, "session_end")
    target, state = _continue_from(source_state, monkeypatch)
    assert state["name"] == "carried"
    # `work name` accepts the same label on the second record.
    assert cli.cmd_name("carried") == 0
    # A second continuation of the same source also carries it.
    monkeypatch.delenv("LMER_SOURCE_SESSION_ID", raising=False)
    target2, state2 = _continue_from(source_state, monkeypatch)
    assert target2 != target and state2["name"] == "carried"
    assert "not applied" not in capsys.readouterr().out


def test_work_name_and_seed_and_launch_name_all_accept_a_held_label(
    session_tree, monkeypatch
):
    from work_repo import cli, session_record

    _, holder, holder_state = seed_launch()
    holder_state["name"] = "taken"
    run_state.write_state(holder, holder_state)
    session_record.start_launch()
    rdir, state = run_state.ensure_run()
    assert cli.cmd_name("taken") == 0
    assert run_state.load_state(rdir)["name"] == "taken"
    # `find_run_dir(name, match_names=True)` never resolves by name on schema 2.
    assert run_state.find_run_dir("taken", match_names=True) is None


def test_seeded_record_helpers_are_noops_on_a_legacy_tree(tmp_path, monkeypatch):
    from work_repo import run_state

    monkeypatch.setattr(run_state, "is_process_record", lambda: False)
    rdir = tmp_path / "legacy-run"
    rdir.mkdir()
    run_state.note_seeded_record(rdir)
    assert run_state.seeded_record_rel_paths() == []
    assert not (rdir / "state.yaml").exists()


def test_launcher_replaces_inherited_uid_once_after_schema_detection(session_tree, monkeypatch):
    from lmer_cli.container import clone_and_exec

    monkeypatch.setenv("LMER_SESSION_ID", "a" * 32)
    clone_and_exec.mint_session_id(fresh=True)
    first = os.environ["LMER_SESSION_ID"]
    assert first != "a" * 32
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    clone_and_exec.mint_session_id()
    assert os.environ["LMER_SESSION_ID"] == first
    metadata = json.loads(os.environ["LMER_SESSION_METADATA"])
    assert metadata["uid"] == first


def test_launcher_uses_v2_identity_when_registry_is_unusable(session_tree, monkeypatch):
    from lmer_cli.container import clone_and_exec

    (session_tree / "_plexus.yaml").write_text(
        "schema: 2\nname: example\nprojects:\n  bad:\n"
        "    remotes: [git.example.test/Team/Repo]\n"
    )
    monkeypatch.setenv("LMER_REPO_URL", "https://git.example.test:8443/Team/Repo")
    placement = clone_and_exec._resolve_work_repo_placement(
        session_tree, "git.example.test:8443", "Team/Repo"
    )
    assert placement.base == session_tree / "git.example.test%3A8443" / "Team" / "Repo"
    assert placement.problem


def test_launcher_refuses_ambiguous_unregistered_endpoint(session_tree, monkeypatch):
    from lmer_cli.container import clone_and_exec

    (session_tree / "git.example.test" / "Team" / "Repo" / "_runs").mkdir(parents=True)
    monkeypatch.setenv("LMER_REPO_URL", "https://git.example.test:8443/Team/Repo")
    placement = clone_and_exec._resolve_work_repo_placement(
        session_tree, "git.example.test:8443", "Team/Repo"
    )
    assert placement.base is None
    assert "ambiguous" in placement.problem


def test_container_refuses_unsigned_source_before_runner_dispatch(session_tree, tmp_path):
    from tests.test_clone_and_exec_no_repo import _run_main

    old, source, _ = seed_launch()
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    launch_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "LMER_WORK_REPO": "https://git.example.test/acme/work",
        "LMER_WORK_REPO_PATH": str(session_tree),
        "LMER_REPO_URL": "https://git.example.test/acme/widgets",
        "LMER_REPO_HOST": "git.example.test",
        "LMER_REPO_PROJECT": "acme/widgets",
        "LMER_TASK": "chat",
        "LMER_SESSION_ID": old["uid"],
        "LMER_SESSION_IS_SOURCE": "1",
    }
    rc, _clones, dispatches = _run_main(launch_env)
    assert rc == 2
    assert dispatches == []
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == before
    assert list(source.parent.iterdir()) == [source]


def test_continuation_carries_checkpoint_without_reopening_source(session_tree, monkeypatch):
    from work_repo import release_run, session_record

    old, source, state = seed_launch()
    state["goal"] = "Continue the release"
    run_state.write_state(source, state)
    run_state.write_ledger(source, {"schema": 1, "tasks": {"T1": {"status": "done"}}})
    release_run.write_release(source, {"schema": 1, "version": "1.2.3"})
    run_state.append_event(source, "session_end")
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    session_record.start_launch()
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    destination, continued = run_state.ensure_run()
    assert continued["goal"] == "Continue the release"
    assert run_state.load_ledger(destination)["tasks"]["T1"]["status"] == "done"
    assert release_run.load_release(destination)["version"] == "1.2.3"
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == before


def test_bad_foreign_record_is_not_renamed_by_reading_it(session_tree):
    from work_repo import session_record

    _, source, _ = seed_launch()
    path = source / "state.yaml"
    path.write_text("[invalid")
    session_record.start_launch()
    with pytest.raises(run_state.RunStateError):
        run_state.load_state(source)
    assert path.read_text() == "[invalid"
    assert not list(source.glob("*.bad-*"))


def test_source_uid_finds_its_record_after_group_rename(session_tree, monkeypatch):
    from work_repo import session_record

    old, source, state = seed_launch()
    moved, state, _ = run_state.reslug_run(source, state, "chat-v1")
    run_state.append_event(moved, "session_end")
    seed_launch()  # a later execution now owns the original chat group
    session_record.start_launch()
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    _, continued = run_state.ensure_run()
    assert continued["context"]["source_path"] == moved.relative_to(session_tree).as_posix()


def test_opaque_listing_uses_start_time_and_records_signout(session_tree):
    from work_repo import session_record

    first, directory, state = seed_launch()
    state["created"] = state["session"]["started_at"] = "1900-01-01T00:00:00Z"
    state["owner"] = {"session_id": first["uid"], "claimed_at": state["created"]}
    run_state.write_state(directory, state)
    run_state.append_event(directory, "session_start")
    row = run_state.list_sessions()[0]
    assert row["signed_out"] is False
    assert row["orphaned"] is None  # no inference of death from the timestamp
    run_state.append_event(directory, "session_end")
    assert run_state.list_sessions()[0]["signed_out"] is True


def test_pushed_answer_belongs_to_new_execution_not_source(session_tree, monkeypatch):
    from work_repo import cli, session_record

    old, source, state = seed_launch()
    state.update(open_question="Proceed?", stop_reason="question")
    run_state.write_state(source, state)
    run_state.append_event(source, "session_end")
    before = (source / "state.yaml").read_bytes()
    monkeypatch.setattr(cli, "ANSWER_MARKER_DIR", str(session_tree))
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    monkeypatch.setenv("LMER_ANSWER", "Proceed now")
    for _ in range(2):
        session_record.start_launch()
        assert cli.cmd_session_start() == 0
        destination = run_state.run_dir()
        answers = [e for e in run_state.read_events(destination, last_n=0)
                   if e.get("type") == "question_answered"]
        assert answers[-1]["data"]["answer"] == "Proceed now"
        assert run_state.load_state(destination)["open_question"] is None
    assert (source / "state.yaml").read_bytes() == before


def _snapshot_record(path):
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def _run_container_session_start(launch_env, answer_markers):
    """Enter through the real container handshake and real work session start."""
    from lmer_cli.container import clone_and_exec
    from work_repo import cli

    captured = {}

    def preserve_planted_repositories(*_args, **_kwargs):
        return None

    def run_stubbed_harness(_runner):
        captured["writer_uid"] = os.environ["LMER_SESSION_ID"]
        captured["source_uid"] = os.environ.get("LMER_SOURCE_SESSION_ID")
        captured["session_start_rc"] = cli.cmd_session_start()
        captured["record"] = run_state.run_dir()
        return captured["session_start_rc"]

    answer_markers.mkdir(parents=True, exist_ok=True)
    with (
        patch.dict(os.environ, launch_env, clear=True),
        patch.object(clone_and_exec, "ensure_clone", preserve_planted_repositories),
        patch.object(clone_and_exec, "provision_documentation"),
        patch.object(clone_and_exec, "clone_aux_repos"),
        patch.object(clone_and_exec, "setup_napkin_and_links"),
        patch.object(clone_and_exec, "find_runner", return_value="/bin/true"),
        patch.object(clone_and_exec, "dispatch_runner", run_stubbed_harness),
        patch.object(cli, "ANSWER_MARKER_DIR", str(answer_markers)),
    ):
        captured["main_rc"] = clone_and_exec.main(["--", "claude-runner"])
    return captured


@pytest.mark.parametrize("signed_out", [True, False], ids=["signed", "exact-ack"])
def test_container_accepted_continuation_is_fresh_and_read_only(
    session_tree, tmp_path, signed_out
):
    from work_repo import session_record

    old, source, source_state = seed_launch()
    source_state.update(open_question="Proceed?", stop_reason="question")
    run_state.write_state(source, source_state)
    if signed_out:
        run_state.append_event(source, "session_end")

    renamed_group = source.parent.with_name("renamed-source-group")
    source.parent.rename(renamed_group)
    source = renamed_group / source.name
    before = _snapshot_record(source)

    host_metadata = session_record.compose_launch_metadata(
        {
            "operator": "host-user",
            "hostname": "host.example",
            "model": "predecessor-model",
            "config": {"api_token": "private-value", "future": {"x": 3}},
            "extension": {"kept": [1, 2]},
        },
        harness="codex",
        model="current-model",
        effort="high",
        config={"preset": "review"},
    )
    launch_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "LMER_WORK_REPO": "https://git.example.test/acme/work",
        "LMER_WORK_REPO_PATH": str(session_tree),
        "LMER_REPO_URL": "https://git.example.test/acme/widgets",
        "LMER_REPO_HOST": "git.example.test",
        "LMER_REPO_PROJECT": "acme/widgets",
        "LMER_TASK": "chat",
        "LMER_TASK_TARGET": "",
        "LMER_SESSION_ID": old["uid"],
        "LMER_SESSION_IS_SOURCE": "1",
        "LMER_SESSION_METADATA": json.dumps(host_metadata),
        "LMER_HARNESS": "codex",
        "LMER_LLM_NAME": "current-model",
        "LMER_REASONING_EFFORT": "high",
        "LMER_ANSWER": "Proceed in the fresh execution",
    }
    if not signed_out:
        launch_env["LMER_ALLOW_UNSIGNED_SOURCE"] = old["uid"]

    result = _run_container_session_start(
        launch_env, tmp_path / f"answer-markers-{signed_out}"
    )

    assert result["main_rc"] == result["session_start_rc"] == 0
    assert result["source_uid"] == old["uid"]
    assert re.fullmatch(r"[0-9a-f]{32}", result["writer_uid"])
    assert result["writer_uid"] != old["uid"]
    assert _snapshot_record(source) == before

    destination = result["record"]
    assert destination != source
    assert destination.name == result["writer_uid"]
    state = run_state.load_state(destination)
    assert state["context"]["source_session_uid"] == old["uid"]
    assert state["context"]["source_path"] == source.relative_to(session_tree).as_posix()
    assert state["session"].get("unsigned_source_acknowledged") == (
        None if signed_out else old["uid"]
    )
    assert state["open_question"] is None
    assert state["stop_reason"] is None
    events = run_state.read_events(destination, last_n=0)
    answers = [event for event in events if event.get("type") == "question_answered"]
    assert answers[-1]["data"]["answer"] == "Proceed in the fresh execution"
    starts = [event for event in events if event.get("type") == "session_start"]
    final_metadata = starts[-1]["data"]["session"]
    assert final_metadata["uid"] == result["writer_uid"]
    assert final_metadata["harness"] == "codex"
    assert final_metadata["model"] == "current-model"
    assert final_metadata["effort"] == "high"
    assert final_metadata["config"]["preset"] == "review"
    assert final_metadata["config"]["future"] == {"x": 3}
    assert final_metadata["config"]["api_token"] == "***REDACTED***"
    assert final_metadata["extension"] == {"kept": [1, 2]}
    assert "private-value" not in json.dumps(final_metadata)


def test_container_schema_one_keeps_inherited_session_writer(
    session_tree, tmp_path, monkeypatch
):
    (session_tree / "_plexus.yaml").write_text(
        "schema: 1\nname: example\nprojects:\n  widgets:\n"
        "    repo: git.example.test/acme/widgets\n"
    )
    legacy_session = "20260917-120000-1-2"
    monkeypatch.setenv("LMER_SESSION_ID", legacy_session)
    source, _state = run_state.ensure_run()
    launch_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "LMER_WORK_REPO": "https://git.example.test/acme/work",
        "LMER_WORK_REPO_PATH": str(session_tree),
        "LMER_REPO_URL": "https://git.example.test/acme/widgets",
        "LMER_REPO_HOST": "git.example.test",
        "LMER_REPO_PROJECT": "acme/widgets",
        "LMER_TASK": "chat",
        "LMER_TASK_TARGET": "",
        "LMER_SESSION_ID": legacy_session,
        "LMER_SESSION_IS_SOURCE": "1",
    }

    result = _run_container_session_start(
        launch_env, tmp_path / "schema-one-answer-markers"
    )

    assert result["main_rc"] == result["session_start_rc"] == 0
    assert result["writer_uid"] == legacy_session
    assert result["source_uid"] is None
    assert result["record"] == source
    assert [path.name for path in source.parent.iterdir()] == [legacy_session]
    assert "session_start" in {
        event["type"] for event in run_state.read_events(source, last_n=0)
    }


def test_a_bare_shell_answer_is_refused_and_names_the_platform_route(
    session_tree, monkeypatch, capsys
):
    """A schema-2 record belongs to a launch, so `work answer` from a shell
    with no session UID stays refused — and the refusal tells the human where
    an answer goes instead of leaving them at a dead end."""
    import sys

    from work_repo import cli

    _, source, state = seed_launch()
    state.update(stop_reason="question", open_question="ship it?")
    run_state.write_state(source, state)
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    monkeypatch.delenv("LMER_SESSION_ID")
    monkeypatch.delenv("LMER_SESSION_METADATA", raising=False)
    monkeypatch.setattr(sys, "argv", ["work", "answer", "yes, ship it"])
    assert cli.main() == 2
    err = capsys.readouterr().err
    assert "Session write refused" in err
    assert "POST /api/runs/answer" in err and "answer card" in err
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == before


# ---------------------------------------------------------------------------
# lmer --name / --goal applied once at session start; local-path continuations
# ---------------------------------------------------------------------------


def _events_of(directory, kind):
    return [e for e in run_state.read_events(directory, last_n=0) if e.get("type") == kind]


def test_launch_name_and_goal_apply_once_on_the_opening_session_start(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_RUN_NAME", "Fresh Run")
    monkeypatch.setenv("LMER_RUN_GOAL", "Finish the index")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    directory = run_state.run_dir()
    state = run_state.load_state(directory)
    assert state["name"] == "fresh-run"
    assert state["goal"] == "Finish the index"
    (named,) = _events_of(directory, "run_named")
    assert named["note"] == "fresh-run" and named["data"] == {"source": "launch"}
    (goal_set,) = _events_of(directory, "goal_set")
    assert goal_set["note"] == "Finish the index" and goal_set["data"] == {"source": "launch"}
    assert "not applied" not in capsys.readouterr().out

    # The same launch starting again applies nothing and warns nothing.
    assert cli.cmd_session_start() == 0
    assert len(_events_of(directory, "run_named")) == 1
    assert len(_events_of(directory, "goal_set")) == 1
    assert "not applied" not in capsys.readouterr().out


def test_a_launch_name_is_a_label_and_never_conflicts_on_schema_two(
    session_tree, monkeypatch, capsys
):
    """D4: on schema 2 a name is a label, so a launch name equal to another
    record's label applies; the legacy uniqueness scan is never consulted."""
    from work_repo import cli, session_record

    _, holder, holder_state = seed_launch()
    holder_state["name"] = "taken"
    run_state.write_state(holder, holder_state)
    monkeypatch.setenv("LMER_RUN_NAME", "taken")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    out = capsys.readouterr().out
    assert "not applied" not in out
    directory = run_state.run_dir()
    assert directory != holder
    assert run_state.load_state(directory)["name"] == "taken"
    named = _events_of(directory, "run_named")
    assert len(named) == 1 and named[0]["note"] == "taken"
    assert named[0]["data"]["source"] == "launch"


def test_an_invalid_launch_name_warns_and_the_run_stays_unnamed(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_RUN_NAME", "###")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    out = capsys.readouterr().out
    assert "launch name '###' not applied" in out
    assert " owns " in out
    directory = run_state.run_dir()
    assert run_state.is_default_label(run_state.load_state(directory))
    assert _events_of(directory, "run_named") == []


def test_name_conflict_is_never_scanned_on_schema_two(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_RUN_NAME", "fresh-run")
    monkeypatch.setenv("LMER_RUN_GOAL", "Finish the index")

    def boom(*_args, **_kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(cli, "_name_conflict", boom)
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    out = capsys.readouterr().out
    assert "scan exploded" not in out
    directory = run_state.run_dir()
    state = run_state.load_state(directory)
    assert state["name"] == "fresh-run"
    named = _events_of(directory, "run_named")
    assert len(named) == 1 and named[0]["note"] == "fresh-run"


def test_a_raising_state_write_is_a_warning_not_a_refusal(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_RUN_NAME", "fresh-run")
    monkeypatch.setenv("LMER_RUN_GOAL", "Finish the index")
    real_write = run_state.write_state

    def refuse_the_identity_write(rdir, state):
        if state.get("name") == "fresh-run":
            raise RuntimeError("disk said no")
        real_write(rdir, state)

    monkeypatch.setattr(run_state, "write_state", refuse_the_identity_write)
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    out = capsys.readouterr().out
    assert "⚠️  launch name/goal not applied (continuing): disk said no" in out
    assert " owns " in out
    directory = run_state.run_dir()
    state = run_state.load_state(directory)
    assert run_state.is_default_label(state) and state["goal"] is None
    assert state["owner"]["session_id"] == os.environ["LMER_SESSION_ID"]
    assert _events_of(directory, "run_named") == []
    assert _events_of(directory, "goal_set") == []


def test_a_continuation_inherits_the_source_goal_unless_a_launch_goal_is_given(
    session_tree, monkeypatch
):
    from work_repo import cli, session_record

    old, source, source_state = seed_launch()
    source_state["goal"] = "Finish the existing work"
    run_state.write_state(source, source_state)
    run_state.append_event(source, "session_end")
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])

    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    inherited = run_state.run_dir()
    assert run_state.load_state(inherited)["goal"] == "Finish the existing work"
    assert _events_of(inherited, "goal_set") == []

    monkeypatch.setenv("LMER_RUN_GOAL", "Take it somewhere else")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    replaced = run_state.run_dir()
    assert replaced != inherited
    assert run_state.load_state(replaced)["goal"] == "Take it somewhere else"
    (goal_set,) = _events_of(replaced, "goal_set")
    assert goal_set["note"] == "Take it somewhere else"
    assert goal_set["data"] == {"source": "launch"}


# What `hooks/start.py` hands the agent is session-start's STDOUT, so the
# launch name and goal must be in it — persisting them to state.yaml alone
# leaves the agent naming the run again or working from a predecessor's goal.
LAUNCH_HEADER = "This launch (lmer --name/--goal, already recorded):"


def test_a_fresh_launch_prints_its_launch_name_and_goal_for_the_hook(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_RUN_NAME", "Fresh Run")
    monkeypatch.setenv("LMER_RUN_GOAL", "Finish the index")
    fresh = session_record.start_launch()
    assert cli.cmd_session_start() == 0
    lines = capsys.readouterr().out.splitlines()
    assert not any(line.startswith("Context from session") for line in lines)
    launch_at = lines.index(LAUNCH_HEADER)
    assert lines[launch_at:launch_at + 3] == [
        LAUNCH_HEADER, "Run name: fresh-run", "Goal: Finish the index",
    ]
    assert lines[launch_at + 3].startswith(f"Session {fresh['uid']} owns ")

    # A later start of the same launch applied nothing, so it announces nothing.
    assert cli.cmd_session_start() == 0
    assert LAUNCH_HEADER not in capsys.readouterr().out


def test_a_continuation_brief_makes_the_launch_goal_current(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    old, source, source_state = seed_launch()
    source_state.update(goal="OLD goal", phase="build")
    run_state.write_state(source, source_state)
    run_state.append_event(source, "session_end")
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    monkeypatch.setenv("LMER_RUN_NAME", "elsewhere")
    monkeypatch.setenv("LMER_RUN_GOAL", "NEW continuation goal")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    out = capsys.readouterr().out
    lines = out.splitlines()

    # The predecessor stays read-only context: its phase is still shown...
    context_at = lines.index(f"Context from session {old['uid']} (read-only):")
    assert any(line.startswith("Phase: build") for line in lines[context_at:])
    # ...but its goal is never presented as the goal: the launch goal is the
    # only `Goal:` line, and the old one survives only labelled as replaced.
    assert [line for line in lines if line.startswith("Goal:")] == [
        "Goal: NEW continuation goal"
    ]
    launch_at = lines.index(LAUNCH_HEADER)
    assert launch_at > context_at
    assert lines[launch_at:launch_at + 4] == [
        LAUNCH_HEADER,
        "Run name: elsewhere",
        "Goal: NEW continuation goal",
        "Predecessor goal (context only, replaced by the launch goal): OLD goal",
    ]
    assert " owns " in lines[-1]
    state = run_state.load_state(run_state.run_dir())
    assert state["goal"] == "NEW continuation goal"
    assert state["name"] == "elsewhere"


def test_a_continuation_without_a_launch_goal_keeps_the_inherited_goal_current(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    old, source, source_state = seed_launch()
    source_state["goal"] = "OLD goal"
    run_state.write_state(source, source_state)
    run_state.append_event(source, "session_end")
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    monkeypatch.setenv("LMER_RUN_NAME", "carried-on")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line.startswith("Goal:")] == ["Goal: OLD goal"]
    launch_at = lines.index(LAUNCH_HEADER)
    assert lines[launch_at + 1] == "Run name: carried-on"
    assert not any(line.startswith("Predecessor goal") for line in lines)


def test_a_launch_name_lands_beside_the_goal_on_schema_two(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    _, holder, holder_state = seed_launch()
    holder_state["name"] = "taken"
    run_state.write_state(holder, holder_state)
    monkeypatch.setenv("LMER_RUN_NAME", "taken")
    monkeypatch.setenv("LMER_RUN_GOAL", "Finish the index")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    lines = capsys.readouterr().out.splitlines()
    launch_at = lines.index(LAUNCH_HEADER)
    assert lines[launch_at + 1] == "Run name: taken"
    assert lines[launch_at + 2] == "Goal: Finish the index"
    assert " owns " in lines[-1]


def test_a_launch_name_alone_prints_the_launch_lines(session_tree, monkeypatch, capsys):
    from work_repo import cli, session_record

    _, holder, holder_state = seed_launch()
    holder_state["name"] = "taken"
    run_state.write_state(holder, holder_state)
    monkeypatch.setenv("LMER_RUN_NAME", "taken")
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    out = capsys.readouterr().out
    assert LAUNCH_HEADER in out
    assert "Run name: taken" in out


def test_a_continuation_of_a_dotted_source_lands_under_the_bare_taskdef(
    session_tree, monkeypatch, capsys
):
    """A `chat-.` record (a local-path launch from a host that exported `.`,
    filed before run-identity's A1 wave 0, when the derivation still spelled
    it that way) is continued under `chat`: the new record keeps its
    provenance into the old group, the old group is left exactly as it was,
    and `work session-start` says what happened. The old record is planted
    under its old spelling explicitly — the live derivation now files `.`
    under the bare taskdef in both copies (spec §3)."""
    from work_repo import cli, session_record

    monkeypatch.setenv("LMER_TASK_TARGET", ".")
    old = session_record.start_launch()
    source, _ = run_state.ensure_run(slug="chat-.")
    assert source.parent == session_tree / "widgets" / "_runs" / "chat-."
    run_state.append_event(source, "session_end")
    before = _snapshot_record(source)

    monkeypatch.setenv("LMER_TASK_TARGET", "")
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    fresh = session_record.start_launch()
    assert cli.cmd_session_start() == 0
    out = capsys.readouterr().out

    destination = run_state.run_dir()
    assert destination == session_tree / "widgets" / "_runs" / "chat" / fresh["uid"]
    state = run_state.load_state(destination)
    assert state["slug"] == "chat"
    assert state["target"] == ""
    assert state["session"]["source_session_uid"] == old["uid"]
    assert state["context"]["source_path"] == f"widgets/_runs/chat-./{old['uid']}"
    assert _snapshot_record(source) == before
    assert (
        f"⚠️  continuing session {old['uid']} recorded under slug 'chat-.'; "
        "this execution is filed under 'chat' (the old group stays as history)"
    ) in out


def test_a_continuation_within_one_group_prints_no_slug_warning(
    session_tree, monkeypatch, capsys
):
    from work_repo import cli, session_record

    old, source, _ = seed_launch()
    run_state.append_event(source, "session_end")
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    session_record.start_launch()
    assert cli.cmd_session_start() == 0
    assert "recorded under slug" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `work seed` writes a fresh execution identity (#535) and a seeded record
# nobody has run in is `seeded` (D13).
# ---------------------------------------------------------------------------


def _work(monkeypatch, *argv):
    import sys

    from work_repo import cli

    monkeypatch.setattr(sys, "argv", ["work", *argv])
    return cli.main()


def test_work_seed_never_reuses_the_launch_uid(session_tree, monkeypatch):
    """Issue #535: a seeded record is not this launch's record. Reusing the
    launch UID gave it two records, so its own run_dir() could no longer be
    resolved."""
    from work_repo import session_record

    metadata, own, _ = seed_launch()

    assert _work(monkeypatch, "seed", "develop", "other-target") == 0

    assert run_state.run_dir() == own
    seeded = run_state.find_run_dir("develop-other-target")
    assert seeded is not None and seeded != own
    assert session_record.UID_RE.fullmatch(seeded.name)
    assert seeded.name != metadata["uid"]
    seeded_state = run_state.load_state(seeded)
    assert seeded_state["session"]["uid"] == seeded.name
    assert metadata["uid"] not in run_state.session_lineage(seeded_state)


def _runs_area(session_tree):
    return session_tree / "widgets" / "_runs"


def test_work_seed_record_carries_only_its_seed_identity(session_tree, monkeypatch):
    metadata, _, _ = seed_launch()
    assert _work(monkeypatch, "seed", "develop", "other-target") == 0
    seeded = run_state.find_run_dir("develop-other-target")
    state = run_state.load_state(seeded)
    assert set(state["session"]) == {"uid", "seeded_by", "started_at"}
    assert state["session"]["uid"] == seeded.name
    assert state["session"]["seeded_by"] == metadata["uid"]
    assert isinstance(state["session"]["started_at"], str) and state["session"]["started_at"]
    assert run_state.session_lineage(state) == ()


def test_work_seed_writes_goal_and_name_before_the_single_rename(session_tree, monkeypatch):
    seed_launch()
    assert _work(
        monkeypatch, "seed", "develop", "other-target",
        "--goal", "Seed a later launch", "--name", "seeded-name",
    ) == 0
    seeded = run_state.find_run_dir("develop-other-target")
    state = run_state.load_state(seeded)
    assert state["goal"] == "Seed a later launch"
    assert state["name"] == "seeded-name"
    events = run_state.read_events(seeded, last_n=0)
    assert [e["type"] for e in events] == ["run_seeded", "goal_set", "run_named"]
    assert [e.get("note") for e in events[1:]] == ["Seed a later launch", "seeded-name"]
    assert [p.name for p in _runs_area(session_tree).iterdir() if p.name.startswith(".new-")] == []


def test_work_seed_adds_a_second_record_to_a_held_group(session_tree, monkeypatch, capsys):
    seed_launch()
    assert _work(monkeypatch, "seed", "develop", "other-target") == 0
    first = run_state.find_run_dir("develop-other-target")
    assert "already holds" not in capsys.readouterr().out
    assert _work(monkeypatch, "seed", "develop", "other-target") == 0
    assert "group develop-other-target already holds 1 record(s)" in capsys.readouterr().out
    group = _runs_area(session_tree) / "develop-other-target"
    records = sorted(p.name for p in group.iterdir() if not p.name.startswith("."))
    assert len(records) == 2 and first.name in records
    assert all((group / name / "state.yaml").is_file() for name in records)
    assert all(run_state.load_state(group / name)["slug"] == "develop-other-target" for name in records)


def test_work_seed_accepts_an_existing_empty_group_dir(session_tree, monkeypatch, capsys):
    seed_launch()
    group = _runs_area(session_tree) / "develop-other-target"
    group.mkdir()
    assert _work(monkeypatch, "seed", "develop", "other-target") == 0
    captured = capsys.readouterr()
    assert "already exists" not in captured.err
    assert "already holds" not in captured.out
    seeded = run_state.find_run_dir("develop-other-target")
    assert seeded is not None and seeded.parent == group


def test_work_seed_prints_the_uid_and_the_launch_line(session_tree, monkeypatch, capsys):
    seed_launch()
    assert _work(monkeypatch, "seed", "develop", "feature branch") == 0
    out = capsys.readouterr().out
    seeded = run_state.find_run_dir("develop-feature-branch")
    assert seeded.name in out
    assert f"lmer develop 'feature branch' --source-session {seeded.name}" in out
    assert "work commit" in out
    # An empty target is left out of the launch line, not quoted as ''.
    assert _work(monkeypatch, "seed", "review", "") == 0
    out = capsys.readouterr().out
    seeded = run_state.find_run_dir("review")
    assert f"lmer review --source-session {seeded.name}" in out
    assert "''" not in out


def test_work_seed_record_is_read_only_to_the_seeder_afterwards(session_tree, monkeypatch):
    from work_repo import cli

    _, own, own_state = seed_launch()
    assert _work(monkeypatch, "seed", "develop", "other-target", "--goal", "Seed a later launch") == 0
    seeded = run_state.find_run_dir("develop-other-target")
    before = _snapshot_record(seeded)
    # The launch carries on in its own record...
    own_state["goal"] = "The launch's own goal"
    run_state.write_state(own, own_state)
    run_state.append_event(own, "goal_set", note=own_state["goal"])
    assert cli.cmd_session_start() == 0
    assert run_state.run_dir() == own
    assert _snapshot_record(seeded) == before
    # ...and can no longer write the record it seeded, at its final address.
    seeded_state = run_state.load_state(seeded)
    seeded_state["goal"] = "Rewritten by the seeder"
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.write_state(seeded, seeded_state)
    with pytest.raises(run_state.RunStateError, match="own"):
        run_state.append_event(seeded, "goal_set", note="Rewritten by the seeder")
    assert _snapshot_record(seeded) == before


def test_work_seed_record_validates_clean(session_tree, monkeypatch):
    from lmer_schema.checks.runner import validate

    seed_launch()
    assert _work(
        monkeypatch, "seed", "develop", "other-target",
        "--goal", "Seed a later launch", "--name", "seeded-name",
    ) == 0
    report = validate(session_tree)
    assert report.errors == ()
    assert not [f for f in report.findings if f.code.startswith("record.")]


# A seeded record nobody has run in yet is `seeded` (D13): one lifecycle fact
# every reader shares — the source check, the listing and `--running`.

_SEEDED = {"uid": "b" * 32, "seeded_by": "c" * 32, "started_at": "2026-10-07T00:00:00+00:00"}
_LAUNCHED = {"uid": "b" * 32, "started_at": "2026-10-07T00:00:00+00:00"}


@pytest.mark.parametrize("session, events, seeded, stopped", [
    (_SEEDED, [], True, True),
    (_SEEDED, [{"type": "run_seeded"}, {"type": "goal_set"}], True, True),
    (_SEEDED, [{"type": "session_start"}], False, False),
    (_SEEDED, [{"type": "session_start"}, {"type": "session_end"}], False, True),
    ({**_LAUNCHED, "seeded_by": ""}, [], False, False),
    (_LAUNCHED, [], False, False),
    (_LAUNCHED, [{"type": "session_start"}], False, False),
    (_LAUNCHED, [{"type": "session_start"}, {"type": "session_end"}], False, True),
], ids=[
    "seeded", "seeded-with-seed-events", "seeded-by-then-started",
    "seeded-by-then-signed-out", "empty-seeded-by", "fresh", "started", "signed-out",
])
def test_is_seeded_and_is_stopped_share_one_lifecycle_rule(session, events, seeded, stopped):
    from work_repo import session_record

    state = {"schema": 2, "session": session}
    assert session_record.is_seeded(state, events) is seeded
    assert session_record.is_stopped(state, events) is stopped


def _seed_for_a_later_launch(monkeypatch):
    """`work seed` from a launch of its own, then point the environment at
    the launch line it printed: the seeded group, the seed as source."""
    seed_launch()
    assert _work(
        monkeypatch, "seed", "develop", "other-target", "--goal", "Seed a later launch",
    ) == 0
    seeded = run_state.find_run_dir("develop-other-target")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "other-target")
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", seeded.name)
    monkeypatch.delenv("LMER_ALLOW_UNSIGNED_SOURCE", raising=False)
    return seeded


def test_a_seeded_source_needs_no_unsigned_acknowledgement(session_tree, monkeypatch):
    """Nothing ever ran in a seeded record, so there is no session that
    "may still be running" to acknowledge: the launch the seed was made for
    picks it up as any signed-out source."""
    from work_repo import session_record

    seeded = _seed_for_a_later_launch(monkeypatch)
    before = _snapshot_record(seeded)
    fresh = session_record.start_launch()
    target, state = run_state.ensure_run()
    assert target == seeded.parent / fresh["uid"]
    assert state["context"]["source_session_uid"] == seeded.name
    assert state["session"]["source_session_uid"] == seeded.name
    assert state["goal"] == "Seed a later launch"
    assert "unsigned_source_acknowledged" not in state["session"]
    assert state["context"]["unsigned_source_acknowledged"] is False
    assert _snapshot_record(seeded) == before


def test_an_acknowledgement_a_seeded_source_did_not_need_is_not_recorded(
    session_tree, monkeypatch
):
    from work_repo import session_record

    seeded = _seed_for_a_later_launch(monkeypatch)
    monkeypatch.setenv("LMER_ALLOW_UNSIGNED_SOURCE", seeded.name)
    session_record.start_launch()
    _, state = run_state.ensure_run()
    assert state["context"]["source_session_uid"] == seeded.name
    assert "unsigned_source_acknowledged" not in state["session"]
    assert state["context"]["unsigned_source_acknowledged"] is False


def test_container_accepts_a_seeded_source_without_acknowledgement(
    session_tree, tmp_path, monkeypatch
):
    seeded = _seed_for_a_later_launch(monkeypatch)
    before = _snapshot_record(seeded)
    launch_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "LMER_WORK_REPO": "https://git.example.test/acme/work",
        "LMER_WORK_REPO_PATH": str(session_tree),
        "LMER_REPO_URL": "https://git.example.test/acme/widgets",
        "LMER_REPO_HOST": "git.example.test",
        "LMER_REPO_PROJECT": "acme/widgets",
        "LMER_TASK": "develop",
        "LMER_TASK_TARGET": "other-target",
        "LMER_SOURCE_SESSION_ID": seeded.name,
    }

    result = _run_container_session_start(launch_env, tmp_path / "seeded-answer-markers")

    assert result["main_rc"] == result["session_start_rc"] == 0
    assert result["source_uid"] == seeded.name
    assert re.fullmatch(r"[0-9a-f]{32}", result["writer_uid"])
    assert result["writer_uid"] != seeded.name
    assert result["record"] == seeded.parent / result["writer_uid"]
    state = run_state.load_state(result["record"])
    assert state["context"]["source_session_uid"] == seeded.name
    assert state["goal"] == "Seed a later launch"
    assert "unsigned_source_acknowledged" not in state["session"]
    assert _snapshot_record(seeded) == before


def test_a_started_record_with_seeded_by_still_needs_the_acknowledgement(
    session_tree, monkeypatch
):
    """`seeded_by` alone is not the fact: a `session_start` in the record
    means something ran in it, and the sign-out rule applies as to any
    other record."""
    from work_repo import session_record

    seeder, _, _ = seed_launch()
    uid = "b" * 32
    record = _runs_area(session_tree) / "develop-other-target" / uid
    _plant(record, _seeded_state(uid, seeder["uid"]))
    (record / run_state.EVENTS_FILE).write_text(json.dumps(
        {"ts": "2026-10-07T00:00:00+00:00", "session": uid, "type": "session_start"}
    ) + "\n")
    before = _snapshot_record(record)
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "other-target")
    monkeypatch.setenv("LMER_SOURCE_SESSION_ID", uid)
    new = session_record.start_launch()
    with pytest.raises(run_state.RunStateError, match="sign.out"):
        run_state.ensure_run()
    assert not (record.parent / new["uid"]).exists()
    monkeypatch.setenv("LMER_ALLOW_UNSIGNED_SOURCE", uid)
    target, state = run_state.ensure_run()
    assert target == record.parent / new["uid"]
    assert state["session"]["unsigned_source_acknowledged"] == uid
    assert _snapshot_record(record) == before


def test_listing_marks_the_seeded_record_and_not_the_launch(session_tree, monkeypatch):
    _, own, _ = seed_launch()
    assert _work(monkeypatch, "seed", "develop", "other-target") == 0
    seeded = run_state.find_run_dir("develop-other-target")
    rows = {row["path"]: row for row in run_state.list_sessions()}
    assert rows[seeded]["seeded"] is True
    assert rows[seeded]["signed_out"] is False
    assert rows[own]["seeded"] is False
    assert rows[own]["signed_out"] is False


def test_work_sessions_running_omits_the_seeded_record(session_tree, monkeypatch, capsys):
    _, own, _ = seed_launch()
    assert _work(monkeypatch, "seed", "develop", "other-target") == 0
    seeded = run_state.find_run_dir("develop-other-target")
    capsys.readouterr()
    assert _work(monkeypatch, "sessions", "--running", "--json") == 0
    running = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in running["sessions"]] == [own.name]
    assert _work(monkeypatch, "sessions", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    entries = {entry["id"]: entry for entry in listing["sessions"]}
    assert entries[seeded.name]["seeded"] is True
    assert entries[own.name]["seeded"] is False
    # The table lists the seed too; only `--running` leaves it out.
    assert _work(monkeypatch, "sessions") == 0
    assert seeded.name in capsys.readouterr().out
    assert _work(monkeypatch, "sessions", "--running") == 0
    out = capsys.readouterr().out
    assert own.name in out and seeded.name not in out
