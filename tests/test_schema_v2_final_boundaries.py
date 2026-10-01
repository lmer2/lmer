"""Regression boundaries from the final schema-2 integration review."""

import pytest
import yaml

from tests.test_schema_v2_sessions import session_tree, seed_launch  # noqa: F401
from tests.test_work_repo_memory_scopes import (  # noqa: F401
    schema_tree, schema2_tree, _memory_file, _index_line, _persist,
)
from work_repo import cli, memory, release_run, run_state


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("command", ["log", "report", "artifact"])
def test_future_record_refuses_actual_output_writers(session_tree, tmp_path, command):
    _, directory, state = seed_launch()
    state["schema"] = 3
    (directory / "state.yaml").write_text(yaml.safe_dump(state))
    source = tmp_path / "report.md"
    source.write_text("A report to keep outside the future record.\n")
    before = snapshot(session_tree)
    calls = {
        "log": lambda: cli.cmd_log("A sufficiently descriptive log message", []),
        "report": lambda: cli.cmd_report(str(source)),
        "artifact": lambda: cli.cmd_artifact("report.md", str(source)),
    }
    assert calls[command]() == 1
    assert snapshot(session_tree) == before


@pytest.mark.parametrize("kind", ["ledger", "release"])
@pytest.mark.parametrize("future_on_disk", [True, False])
def test_checkpoint_writer_checks_disk_and_proposed_version(
    session_tree, kind, future_on_disk
):
    _, directory, _ = seed_launch()
    path = directory / f"{kind}.yaml"
    path.write_text(yaml.safe_dump({"schema": 3 if future_on_disk else 1,
                                    "extension": "preserve"}))
    before = snapshot(directory)
    writer = run_state.write_ledger if kind == "ledger" else release_run.write_release
    with pytest.raises(run_state.RunStateError, match="newer|supported"):
        writer(directory, {"schema": 1 if future_on_disk else 3})
    assert snapshot(directory) == before


def test_opaque_newest_record_ignores_newer_backup(session_tree):
    from lmer_platform import workrepo

    group = session_tree / "widgets" / "_runs" / "chat"
    uid = "a" * 32
    for name, date in [(uid, "2026-09-17T00:00:00Z"),
                       ("backup-before-i-broke-it", "2099-01-01T00:00:00Z")]:
        directory = group / name
        directory.mkdir(parents=True)
        (directory / "state.yaml").write_text(yaml.safe_dump({
            "schema": 2, "created": date,
            "session": {"uid": uid, "started_at": date},
        }))
    assert run_state._newest_record(list(group.iterdir())) == group / uid
    assert workrepo._newest_session(group) == group / uid


@pytest.mark.parametrize("scope", ["project", "instance"])
def test_edited_retired_memory_conflicts_before_any_scope_mutates(
    schema2_tree, monkeypatch, capsys, scope
):
    tree, local = schema2_tree
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
    scopes = memory.memory_scope_dirs()
    for name, directory in scopes.items():
        (directory / "review.md").write_text(_memory_file(
            f"{name}-fact", "project" if name == "project" else "user"
        ))
        with (directory / "MEMORY.md").open("a") as handle:
            handle.write(_index_line(name, "review.md"))
    assert memory.restore_memory() == 0
    # Remove the project overlay to exercise the instance baseline as well.
    target = scopes[scope] / "review.md"
    if scope == "instance":
        (scopes["project"] / "review.md").unlink()
        assert memory.restore_memory() == 0
    (local / "review.md").write_text(target.read_text() + "Local correction.\n")
    key = target.relative_to(tree.root).as_posix()
    memory.forget_memory(key)
    before = snapshot(tree.root)
    baseline = memory.restore_manifest_path().read_bytes()
    assert _persist()[0] == 1
    assert "conflict" in capsys.readouterr().err
    assert snapshot(tree.root) == before
    assert memory.restore_manifest_path().read_bytes() == baseline
    assert "Local correction" in (local / "review.md").read_text()


def test_instance_only_restore_and_persist_honor_retirement(schema2_tree, monkeypatch):
    tree, local = schema2_tree
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
    instance = memory.memory_scope_dirs()["instance"]
    retired = instance / "retired.md"
    retired.write_text(_memory_file("retired", "user"))
    (instance / "live.md").write_text(_memory_file("live", "reference"))
    (instance / "MEMORY.md").write_text(
        _index_line("retired", "retired.md") + _index_line("live", "live.md")
    )
    key = retired.relative_to(tree.root).as_posix()
    memory.forget_memory(key)
    retired.write_text(_memory_file("retired", "user"))
    with (instance / "MEMORY.md").open("a") as handle:
        handle.write(_index_line("retired", "retired.md"))
    for key in ("LMER_REPO_HOST", "LMER_REPO_PROJECT", "LMER_REPO_URL"):
        monkeypatch.delenv(key, raising=False)
    assert memory.restore_memory() == 0
    assert not (local / "retired.md").exists()
    assert "(retired.md)" not in (local / "MEMORY.md").read_text()
    assert (local / "live.md").exists()
    assert "live.md" in memory.read_restore_manifest()["instance"]["files"]
    (local / "live.md").write_text(_memory_file("live", "reference", body="Edited\n"))
    assert _persist()[0] == 0
    assert "Edited" in (instance / "live.md").read_text()
    assert not retired.exists()


@pytest.mark.parametrize("retired_scope", ["project", "instance"])
@pytest.mark.parametrize("same_name", [True, False])
def test_retired_scope_keeps_other_scopes_live_same_path_index(
    schema2_tree, monkeypatch, retired_scope, same_name
):
    tree, local = schema2_tree
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
    scopes = memory.memory_scope_dirs()
    for scope, directory in scopes.items():
        (directory / "fact.md").write_text(_memory_file(
            "shared" if same_name else scope,
            "project" if scope == "project" else "reference",
        ))
        (directory / "MEMORY.md").write_text(_index_line(scope, "fact.md"))
    memory.forget_memory((scopes[retired_scope] / "fact.md").relative_to(tree.root).as_posix())
    live_scope = "instance" if retired_scope == "project" else "project"
    assert memory.restore_memory() == 0
    assert _persist()[0] == 0
    assert (scopes[live_scope] / "fact.md").exists()
    assert "(fact.md)" in (scopes[live_scope] / "MEMORY.md").read_text()
    assert not (scopes[retired_scope] / "fact.md").exists()


@pytest.mark.parametrize("retired_scope", ["project", "instance"])
def test_a_tombstone_line_stays_in_its_own_scopes_index(schema2_tree, monkeypatch, retired_scope):
    """forget -> restore into an empty agent dir -> persist with no edits.

    `forget` re-points the index entry at the archive mirror. That line has no
    entry target, and the index split used to copy every such line into BOTH
    scopes — so a project-scope tombstone landed in the instance MEMORY.md,
    where its relative link resolves nowhere (and the reverse for an instance
    one). A tombstone belongs to the scope it was restored from.
    """
    tree, local = schema2_tree
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
    scopes = memory.memory_scope_dirs()
    other_scope = "instance" if retired_scope == "project" else "project"
    kinds = {"project": "project", "instance": "reference"}
    for scope, directory in scopes.items():
        (directory / f"{scope}_fact.md").write_text(_memory_file(f"{scope}-fact", kinds[scope]))
        (directory / f"{scope}_live.md").write_text(_memory_file(f"{scope}-live", kinds[scope]))
        (directory / "MEMORY.md").write_text(
            "# Memory\n"
            + _index_line(f"{scope} fact", f"{scope}_fact.md")
            + _index_line(f"{scope} live", f"{scope}_live.md")
        )
    memory.forget_memory(
        (scopes[retired_scope] / f"{retired_scope}_fact.md").relative_to(tree.root).as_posix()
    )
    tombstone = next(
        line for line in (scopes[retired_scope] / "MEMORY.md").read_text().splitlines()
        if "_archive/" in line
    )
    other_before = (scopes[other_scope] / "MEMORY.md").read_text()

    assert memory.restore_memory() == 0
    assert _persist()[0] == 0

    assert tombstone in (scopes[retired_scope] / "MEMORY.md").read_text()
    other_after = (scopes[other_scope] / "MEMORY.md").read_text()
    assert "_archive/" not in other_after, other_after
    assert other_after == other_before
    # Commentary shared by both indexes is still kept in both.
    assert "# Memory" in other_after and "# Memory" in (scopes[retired_scope] / "MEMORY.md").read_text()


def test_an_instance_forget_does_not_leak_a_live_entry_into_a_project_index(
    schema2_tree, monkeypatch
):
    """S restores; another session forgets an INSTANCE memory; S persists
    with no edits.

    The retired path used to be dropped from the routing map, which left its
    index line unrouted; the split then filed it under the default scope, the
    project, whose retirement filter knows only the project's retired paths.
    Every live session in every project would push that dangling entry into
    its own project index, and each tree then fails ``lmer-schema validate``
    with ``agents.memory-entry-without-file``.
    """
    from lmer_schema.checks.runner import validate

    tree, local = schema2_tree
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
    scopes = memory.memory_scope_dirs()
    name = "user_operator_prefers_terse_replies.md"
    (scopes["instance"] / name).write_text(_memory_file("operator-prefers-terse-replies", "user"))
    (scopes["instance"] / "MEMORY.md").write_text(_index_line("Operator prefers terse replies", name))
    (scopes["project"] / "project_fact.md").write_text(_memory_file("project-fact", "project"))
    (scopes["project"] / "MEMORY.md").write_text(_index_line("Project fact", "project_fact.md"))
    project_before = (scopes["project"] / "MEMORY.md").read_text()

    assert memory.restore_memory() == 0
    assert (local / name).exists()
    memory.forget_memory((scopes["instance"] / name).relative_to(tree.root).as_posix())
    assert _persist()[0] == 0

    project_index = (scopes["project"] / "MEMORY.md").read_text()
    assert f"({name})" not in project_index, project_index
    assert project_index == project_before
    instance_index = (scopes["instance"] / "MEMORY.md").read_text()
    assert f"({name})" not in instance_index and "_archive/" in instance_index
    assert not (scopes["instance"] / name).exists(), "the retired file must not be revived"
    codes = [finding.code for finding in validate(tree.root).findings]
    assert "agents.memory-entry-without-file" not in codes, codes
