"""The platform mints a schema-2 launch's execution uid (spec §6.2, D7) and
surfaces same-project concurrency as a warning rather than a 409 (§6.3, D8).

On a schema-2 mirror a record *is* one execution, so a fresh spawn is a fresh
record and its identity is the uid the platform hands the container as
``--launch-uid``. The platform keys everything it records about the launch by
that uid from the first instant (registry ``run.uid``, ``runs.json``, the meta
and references stores), so nothing has to be re-keyed when the container
reports back. A schema-1 mirror, and a spawn that continues a named schema-1
record (``session_id``), spawn exactly as they did.

Liveness follows the key: a fresh uid can never already be live, so two plain
chats on one project both start, and the second hears about the first in its
``warning`` (#311). A schema-1 run and a replayed ``--session`` continuation
stay one-run-one-session.

Spawned for real against a stub ``lmer`` (as ``tests/test_platform_spawn``
does), on a mirror built by ``tests/schema2_fixtures``.
"""

import os
import re
import time

import pytest
import yaml

from lmer_cli.user_harnesses import HARNESSES_DIR_ENV, clear_user_harness_cache
from lmer_platform import config as cfg
from lmer_platform import meta, registry, runs, spawn, store
from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import build_schema2_plexus

HOST = "gitlab.example.com"
PROJECT = "acme/widgets"
REPO_URL = f"https://{HOST}/{PROJECT}.git"
WORK_ITEM = f"https://{HOST}/{PROJECT}/-/work_items/42"
OTHER_WORK_ITEM = f"https://{HOST}/{PROJECT}/-/work_items/43"

UID_TOKEN = re.compile(r"^--launch-uid=([0-9a-f]{32})$")


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_REPO_URL", "LMER_PLATFORM_PORTS_FILE", "LMER_TASK",
                 "LMER_TASK_TARGET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_user_harnesses(tmp_path, monkeypatch, _clean_lmer_env):
    """The mounts a spawn builds are this checkout's built-ins only."""
    root = tmp_path / "harnesses"
    root.mkdir()
    monkeypatch.setenv(HARNESSES_DIR_ENV, str(root))
    clear_user_harness_cache()
    yield root
    clear_user_harness_cache()


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    """A stub ``lmer``: echoes its argv and stays up while ``FAKE_LMER_SLEEP``."""
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


@pytest.fixture
def schema_two(config):
    """The daemon's mirror is a schema-2 work repo registering ``widgets``."""
    build_schema2_plexus(
        config.mirror_path, repos={"widgets": f"{HOST}/{PROJECT}"},
    )
    return config


@pytest.fixture
def schema_one(config):
    """A schema-1 manifest: records keyed by slug, as they always were."""
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        yaml.safe_dump({
            "schema": 1, "name": "example",
            "projects": {"widgets": {"repo": f"{HOST}/{PROJECT}"}},
        }),
        encoding="utf-8",
    )
    return config


@pytest.fixture
def alive(monkeypatch):
    """Keep every spawned stub up, and kill each one at teardown."""
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    pids = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def chat(**overrides):
    payload = {"taskdef": "chat", "target": None, "repo_url": REPO_URL}
    payload.update(overrides)
    return spawn.SpawnRequest(**payload)


def develop(target=WORK_ITEM, **overrides):
    payload = {"taskdef": "develop", "target": target, "repo_url": REPO_URL}
    payload.update(overrides)
    return spawn.SpawnRequest(**payload)


def launch_uids(command):
    return [m.group(1) for m in map(UID_TOKEN.match, command) if m]


def spawned(config, request, pids, **kwargs):
    result = spawn.spawn_session(config, request, **kwargs)
    pids.append(result.pid)
    return result


# --- Task 68: minting --------------------------------------------------------

def test_a_schema_two_spawn_mints_one_uid_and_keys_everything_by_it(
    schema_two, alive
):
    result = spawned(schema_two, chat(extra_args=("--verbose",)), alive)

    uids = launch_uids(result.command)
    assert len(uids) == 1, result.command
    uid = uids[0]
    token = f"--launch-uid={uid}"
    assert result.command.index(token) < result.command.index("--verbose"), (
        "the uid goes ahead of extra_args, which may end in a bare --"
    )

    entry = registry.read_session(result.session_id)
    assert entry["run"] == {
        "host": HOST, "project": PROJECT, "slug": result.slug, "uid": uid,
    }
    index = runs.load_index()
    key = f"{HOST}/{PROJECT}/{uid}"
    assert key in index, sorted(index)
    assert index[key]["uid"] == uid
    assert index[key]["slug"] == result.slug, "the slug stays as the group label"
    assert runs.run_key(HOST, PROJECT, result.slug) not in index
    assert result.to_dict()["run"]["uid"] == uid


def test_two_schema_two_spawns_mint_two_uids(schema_two, alive):
    first = spawned(schema_two, chat(), alive)
    second = spawned(schema_two, chat(), alive)
    assert launch_uids(first.command) != launch_uids(second.command)


def test_the_title_and_references_are_filed_under_the_uid_key(schema_two, alive):
    result = spawned(schema_two, chat(title="Widgets chat"), alive)
    uid = launch_uids(result.command)[0]
    assert meta.read(HOST, PROJECT, uid).title == "Widgets chat"
    assert result.warning is None


class _Uuid:
    def __init__(self, hex_):
        self.hex = hex_


def _scripted_uuid4(monkeypatch, *values):
    sequence = iter(values)
    monkeypatch.setattr(spawn, "uuid4", lambda: _Uuid(next(sequence)))


def _taken_by_the_index(uid):
    runs.track(HOST, PROJECT, "chat", uid=uid, taskdef="chat")


def _taken_by_a_retained_registry_entry(uid):
    # A dead entry is still evidence the uid was handed out.
    registry.register(
        "s-dead", pid=2**22,
        run={"host": HOST, "project": PROJECT, "slug": "chat", "uid": uid},
    )


@pytest.mark.parametrize("take", [
    _taken_by_the_index, _taken_by_a_retained_registry_entry,
], ids=lambda fn: fn.__name__.lstrip("_"))
def test_a_colliding_first_mint_is_reminted(schema_two, alive, monkeypatch, take):
    taken, fresh = "1" * 32, "2" * 32
    take(taken)
    _scripted_uuid4(monkeypatch, taken, fresh)

    result = spawned(schema_two, chat(), alive)

    assert launch_uids(result.command) == [fresh]
    assert registry.read_session(result.session_id)["run"]["uid"] == fresh


def test_a_mint_that_keeps_colliding_is_refused(schema_two, monkeypatch):
    taken = "3" * 32
    _taken_by_the_index(taken)
    monkeypatch.setattr(spawn, "uuid4", lambda: _Uuid(taken))

    with pytest.raises(spawn.SpawnError, match="uid"):
        spawn.spawn_session(schema_two, chat())
    assert registry.list_sessions(live_only=False) == []


def test_a_schema_one_mirror_emits_no_uid_and_keys_by_slug(schema_one, alive):
    result = spawned(schema_one, chat(), alive)

    assert not any(arg.startswith("--launch-uid") for arg in result.command)
    entry = registry.read_session(result.session_id)
    assert entry["run"] == {"host": HOST, "project": PROJECT, "slug": result.slug}
    assert runs.run_key(HOST, PROJECT, result.slug) in runs.load_index()
    assert "uid" not in result.to_dict()["run"]


def test_a_legacy_mirror_with_no_manifest_emits_no_uid(config, alive):
    result = spawned(config, chat(), alive)
    assert not any(arg.startswith("--launch-uid") for arg in result.command)


def test_a_schema_one_continuation_never_mints(schema_two, alive):
    """``session_id`` names the record to write in, so there is nothing to mint."""
    result = spawned(
        schema_two, develop(session_id="20260824-165230-2-31245"), alive,
    )
    assert not any(arg.startswith("--launch-uid") for arg in result.command)
    assert "uid" not in registry.read_session(result.session_id)["run"]


@pytest.mark.parametrize("arg", [
    "--launch-uid=" + "4" * 32, "--launch-uid", "--launch",
])
def test_launch_uid_in_extra_args_is_refused(schema_two, arg):
    assert "--launch-uid" in spawn._RESERVED_ARGS
    with pytest.raises(spawn.SpawnError, match="not allowed in extra_args"):
        spawn.spawn_session(schema_two, chat(extra_args=(arg,)))
    assert registry.list_sessions(live_only=False) == []


def test_an_unreadable_manifest_is_a_spawn_error(config):
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        "schema: [not, a, number\n", encoding="utf-8",
    )
    with pytest.raises(spawn.SpawnError, match="manifest"):
        spawn.spawn_session(config, chat())
    assert registry.list_sessions(live_only=False) == []


def test_schema_two_reads_the_manifest(config, schema_two):
    assert spawn.schema_two(config) is True


def test_schema_two_is_false_without_a_manifest(config):
    assert spawn.schema_two(config) is False


# --- Task 69: liveness by uid, and the concurrency note ---------------------

def test_two_plain_chats_on_one_schema_two_project_both_start(schema_two, alive):
    """#311: the second plain chat is a second record, not a duplicate session."""
    first = spawned(schema_two, chat(), alive)
    second = spawned(schema_two, chat(), alive)

    assert second.session_id != first.session_id
    warning = second.to_dict()["warning"]
    assert warning is not None
    assert first.session_id in warning
    assert f"`{HOST}/{PROJECT}`" in warning
    assert first.to_dict()["warning"] is None


def test_the_note_rides_beside_a_meta_warning(schema_two, alive, monkeypatch):
    first = spawned(schema_two, chat(), alive)

    def refuse(*_args, **_kwargs):
        raise meta.MetaError("no")

    monkeypatch.setattr(meta, "write", refuse)
    second = spawned(schema_two, chat(title="x"), alive)
    assert "note about it was not stored" in second.warning
    assert first.session_id in second.warning


def _live(session_id, *, target=None, uid=None, slug="chat", pid=None,
          project=PROJECT, kind="worker"):
    run = {"host": HOST, "project": project, "slug": slug}
    if uid is not None:
        run["uid"] = uid
    registry.register(
        session_id, kind=kind, pid=pid or os.getpid(),
        task={"taskdef": "develop", "target": target}, run=run,
    )


def test_the_note_narrows_to_the_same_work_item(platform_root):
    _live("s-same", target=WORK_ITEM, slug="develop-42")
    _live("s-other", target=OTHER_WORK_ITEM, slug="develop-43")
    _live("s-plain")

    note = spawn.concurrent_sessions_note(HOST, PROJECT, WORK_ITEM)

    assert note.startswith(
        f"1 live session(s) already on `{HOST}/{PROJECT}` for `{WORK_ITEM}`: "
    )
    assert "s-same" in note
    assert "s-other" not in note and "s-plain" not in note


def test_the_note_is_project_wide_for_a_target_that_is_not_a_work_item(
    platform_root,
):
    _live("s-one", target=WORK_ITEM, slug="develop-42")
    _live("s-two")
    _live("s-elsewhere", project="acme/other")

    note = spawn.concurrent_sessions_note(HOST, PROJECT, None)

    assert note.startswith(f"2 live session(s) already on `{HOST}/{PROJECT}`: ")
    assert "s-one" in note and "s-two" in note
    assert "s-elsewhere" not in note


def test_a_dead_entry_never_counts_toward_the_note(platform_root):
    _live("s-dead", pid=2**22)
    assert spawn.concurrent_sessions_note(HOST, PROJECT, None) is None


def test_the_assistant_never_counts_toward_the_note(platform_root):
    _live("s-assistant", kind=registry.ASSISTANT_KIND)
    assert spawn.concurrent_sessions_note(HOST, PROJECT, None) is None


def test_no_identity_no_note(platform_root):
    _live("s-one")
    assert spawn.concurrent_sessions_note(None, PROJECT, None) is None
    assert spawn.concurrent_sessions_note(HOST, None, None) is None


def test_live_session_for_run_matches_by_uid_when_given(platform_root):
    uid = "5" * 32
    _live("s-uid", uid=uid)

    found = spawn.live_session_for_run(HOST, PROJECT, "chat", uid=uid)
    assert found["id"] == "s-uid"
    assert spawn.live_session_for_run(HOST, PROJECT, "chat", uid="6" * 32) is None
    # Without a uid the triple matches as it always did.
    assert spawn.live_session_for_run(HOST, PROJECT, "chat")["id"] == "s-uid"


def test_a_schema_one_run_still_raises_run_already_live(schema_one, alive):
    first = spawned(schema_one, chat(), alive)
    with pytest.raises(spawn.RunAlreadyLive):
        spawn.spawn_session(schema_one, chat())
    assert [e["id"] for e in registry.list_sessions(live_only=False)] == [
        first.session_id
    ]


def test_a_schema_one_spawn_carries_no_concurrency_note(schema_one, alive):
    spawned(schema_one, develop(), alive)
    second = spawned(schema_one, develop(OTHER_WORK_ITEM), alive)
    assert second.warning is None


def test_a_replayed_session_continuation_stays_protected(schema_two, alive):
    """``--session`` names one record, so a second writer for it is refused."""
    request = develop(session_id="20260824-165230-2-31245")
    spawned(schema_two, request, alive)
    with pytest.raises(spawn.RunAlreadyLive):
        spawn.spawn_session(schema_two, request)


def test_a_dead_first_chat_leaves_the_second_unwarned(schema_two, monkeypatch):
    first = spawn.spawn_session(schema_two, chat())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and registry.list_sessions(live_only=True):
        time.sleep(0.02)
    assert not registry.list_sessions(live_only=True), first.session_id

    second = spawn.spawn_session(schema_two, chat())
    assert second.warning is None
