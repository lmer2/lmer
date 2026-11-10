"""The daemon's live usage collector (#315): incremental, restart-safe, read-only."""

from __future__ import annotations

import ast
import json
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lmer_platform import config as cfg
from lmer_platform import daemon, registry, spawn, store, transcripts, usage_live
from tests.conftest import strip_lmer_env

FIXTURE = Path(__file__).parent / "fixtures" / "usage" / "claude" / "session-a"
RUN = {"host": "git.example.com", "project": "org/repo", "slug": "masterplan-issue-1"}
DEAD_PID = 2**22 + 12345


@pytest.fixture(autouse=True)
def platform_root(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    harness_root = tmp_path / "harness-transcripts"
    harness_root.mkdir()
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(harness_root))
    return root


def _config(**kw):
    return cfg.PlatformConfig(**kw)


def _plant(session_id, *, operator="tester", files=FIXTURE, pid=DEAD_PID):
    registry.register(session_id, pid=pid, run=dict(RUN),
                      task={"harness": "claude", "operator": operator})
    directory = transcripts.session_transcript_dir(session_id) / "-workspace"
    shutil.copytree(files, directory)
    return directory


def _live(clock=None, **kw):
    now = clock or (lambda: datetime(2026, 11, 5, 10, 5, tzinfo=timezone.utc))
    return usage_live.UsageLive(_config(), clock=now, **kw)


class TestCollection:
    def test_a_session_is_counted_from_its_transcripts(self):
        _plant("s-aaa111")
        live = _live()
        assert live.tick_once() == {"sessions": 1, "changed": 1}
        usage = live.session_usage("s-aaa111")
        assert usage["status"] == "collected"
        assert usage["totals"]["requests"] == 7
        assert usage["run"] == (RUN["host"], RUN["project"], RUN["slug"])
        assert usage["operator"] == "tester"

    def test_a_session_without_transcripts_is_not_collected_never_zero(self):
        registry.register("s-bbb222", pid=DEAD_PID, run=dict(RUN), task={"harness": "pi"})
        live = _live()
        live.tick_once()
        usage = live.session_usage("s-bbb222")
        assert usage["status"] == "not_collected"
        assert usage["reason"] == "no transcript on this host"

    def test_tracked_runs_last_session_is_read_too(self, monkeypatch):
        directory = transcripts.session_transcript_dir("s-ccc333") / "-workspace"
        shutil.copytree(FIXTURE, directory)
        tracked = [SimpleNamespace(host="h", project="p", slug="s", last_session_id="s-ccc333")]
        live = _live(tracked=lambda: tracked)
        live.tick_once()
        assert live.session_usage("s-ccc333")["totals"]["requests"] == 7


class TestRestartAndRewrite:
    def test_a_restart_resumes_from_the_cache_without_recounting(self):
        _plant("s-aaa111")
        first = _live()
        first.tick_once()
        cache = first.store_dir() / "s-aaa111.json"
        assert cache.is_file()
        assert (cache.stat().st_mode & 0o777) == 0o600
        second = _live()
        assert second.tick_once()["changed"] == 0
        assert second.session_usage("s-aaa111")["totals"] == first.session_usage("s-aaa111")["totals"]

    def test_a_rewritten_transcript_is_reread_idempotently(self):
        directory = _plant("s-aaa111", pid=os.getpid())
        live = _live()
        live.tick_once()
        before = live.session_usage("s-aaa111")
        main = directory / "main.jsonl"
        tmp = directory / ".main.tmp"
        tmp.write_bytes(main.read_bytes())
        os.replace(tmp, main)  # new inode, same content: what the scrubber does
        assert live.tick_once()["changed"] == 0
        after = live.session_usage("s-aaa111")
        assert after["totals"] == before["totals"] and after["burn"] == before["burn"]

    def test_a_corrupt_cache_starts_over_rather_than_failing(self):
        _plant("s-aaa111")
        store.ensure_state_dir(store.platform_dir() / usage_live.STORE_DIRNAME)
        (store.platform_dir() / usage_live.STORE_DIRNAME / "s-aaa111.json").write_text("{not json")
        live = _live()
        live.tick_once()
        assert live.session_usage("s-aaa111")["totals"]["requests"] == 7


class TestBurn:
    def test_burn_is_bucketed_by_record_time_not_read_time(self):
        _plant("s-aaa111")
        # Read a day after the fixture's responses: none of them is "now".
        live = _live(clock=lambda: datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))
        live.tick_once()
        usage = live.session_usage("s-aaa111")
        assert usage["totals"]["requests"] == 7
        assert usage["burn"]["requests"] == 0
        assert usage["burn_models"] == []

    def test_recent_responses_are_burn(self):
        _plant("s-aaa111")
        # 2026-11-06T00:05Z is the last response; read five minutes later.
        live = _live(clock=lambda: datetime(2026, 11, 6, 0, 10, tzinfo=timezone.utc))
        live.tick_once()
        burn = live.session_usage("s-aaa111")["burn"]
        assert burn["requests"] == 2  # msg_4 at 00:00:30 and the resumed one at 00:05
        assert burn["output"] == 11 + 13

    def test_a_restart_does_not_dump_history_into_now(self):
        _plant("s-aaa111")
        clock = lambda: datetime(2026, 11, 6, 0, 10, tzinfo=timezone.utc)  # noqa: E731
        _live(clock=clock).tick_once()
        second = _live(clock=clock)
        second.tick_once()
        assert second.session_usage("s-aaa111")["burn"]["requests"] == 2


def _assistant(message_id, timestamp, *, output, model="claude-opus-5-5"):
    return json.dumps({
        "type": "assistant", "timestamp": timestamp, "uuid": f"{message_id}-{output}",
        "message": {"id": message_id, "model": model,
                    "usage": {"input_tokens": 1, "output_tokens": output}},
    }) + "\n"


class TestPublishedViews:
    def test_requests_never_see_a_tick_change_what_they_read(self):
        """API threads copy published views; the collector never mutates one."""
        directory = _plant("s-aaa111", pid=os.getpid())
        live = _live()
        live.tick_once()
        errors, done = [], threading.Event()

        def request():
            while not done.is_set():
                try:
                    live.session_usage("s-aaa111")
                    live.session_events("s-aaa111")
                    live.sessions_by_run()
                except Exception as exc:  # the property under test is "never raises"
                    errors.append(exc)

        readers = [threading.Thread(target=request) for _ in range(3)]
        for thread in readers:
            thread.start()
        try:
            with (directory / "main.jsonl").open("a") as handle:
                for n in range(300):
                    handle.write(_assistant(f"msg_race_{n}", "2026-11-05T10:04:00Z", output=1))
                    handle.flush()
                    live.tick_once()
        finally:
            done.set()
            for thread in readers:
                thread.join()
        assert errors == []
        assert live.session_usage("s-aaa111")["totals"]["requests"] == 7 + 300

    def test_a_published_view_is_not_changed_by_the_next_tick(self):
        directory = _plant("s-aaa111", pid=os.getpid())
        live = _live()
        live.tick_once()
        held = live.session_usage("s-aaa111")
        events = live.session_events("s-aaa111")
        with (directory / "main.jsonl").open("a") as handle:
            handle.write(_assistant("msg_new", "2026-11-05T10:04:00Z", output=9))
        live.tick_once()
        assert held["totals"]["requests"] == 7 and len(events) == 7
        assert live.session_usage("s-aaa111")["totals"]["requests"] == 8

    def test_burn_follows_the_latest_record_of_a_response(self):
        directory = _plant("s-aaa111", pid=os.getpid())
        live = _live()
        with (directory / "main.jsonl").open("a") as handle:
            handle.write(_assistant("msg_grows", "2026-11-05T10:04:00Z", output=10, model="claude-haiku-5-5"))
        live.tick_once()

        def haiku(usage):
            [row] = [r for r in usage["burn_models"] if r["model"] == "claude-haiku-5-5"]
            return row["output"]

        before = live.session_usage("s-aaa111")
        with (directory / "main.jsonl").open("a") as handle:
            handle.write(_assistant("msg_grows", "2026-11-05T10:04:01Z", output=500, model="claude-haiku-5-5"))
        live.tick_once()
        after = live.session_usage("s-aaa111")
        assert haiku(after) - haiku(before) == 490
        assert after["burn"]["output"] - before["burn"]["output"] == 490

    def test_one_request_indexes_sessions_by_run(self):
        _plant("s-aaa111")
        _plant("s-bbb222")
        live = _live()
        live.tick_once()
        key = (RUN["host"], RUN["project"], RUN["slug"])
        assert sorted(live.sessions_by_run()[key]) == ["s-aaa111", "s-bbb222"]


def _user(timestamp):
    return json.dumps({"type": "user", "timestamp": timestamp, "uuid": f"user-{timestamp}",
                       "message": {"role": "user", "content": "next"}}) + "\n"


class TestActivityWithoutUsage:
    def test_a_record_without_usage_moves_the_published_activity(self):
        """User messages and tool results carry no usage but extend the active
        span, and the view, the cache and the settled value all follow."""
        directory = _plant("s-aaa111", pid=os.getpid())
        live = _live()
        live.tick_once()
        before = live.session_usage("s-aaa111")
        ended = datetime.fromisoformat(before["ended_at"].replace("Z", "+00:00"))
        later = (ended + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        with (directory / "main.jsonl").open("a") as handle:
            handle.write(_user(later))
        assert live.tick_once()["changed"] == 1
        after = live.session_usage("s-aaa111")
        assert after["ended_at"].replace("Z", "+00:00") == later.replace("Z", "+00:00")
        assert after["active_seconds"] == before["active_seconds"] + 60
        assert after["totals"] == before["totals"]

        registry.register("s-aaa111", pid=DEAD_PID, run=dict(RUN), task={"harness": "claude"})
        live.tick_once()
        restarted = _live()
        restarted.tick_once()
        assert restarted.session_usage("s-aaa111")["active_seconds"] == after["active_seconds"]


class TestSettledAndForgotten:
    def test_a_session_that_ended_is_read_once_more_and_then_left_alone(self):
        directory = _plant("s-aaa111")
        reads = []
        live = _live(sources=lambda sid: reads.append(sid) or sorted(directory.rglob("*.jsonl")))
        live.tick_once()
        live.tick_once()
        assert reads == ["s-aaa111"]
        assert live.session_usage("s-aaa111")["totals"]["requests"] == 7

    def test_a_session_live_again_is_read_again(self):
        directory = _plant("s-aaa111")
        reads = []
        live = _live(sources=lambda sid: reads.append(sid) or sorted(directory.rglob("*.jsonl")))
        live.tick_once()
        registry.register("s-aaa111", pid=os.getpid(), run=dict(RUN), task={"harness": "claude"})
        live.tick_once()
        assert reads == ["s-aaa111", "s-aaa111"]

    def test_a_session_nobody_names_any_more_is_dropped_with_its_cache(self):
        _plant("s-aaa111")
        _plant("s-bbb222")
        live = _live()
        live.tick_once()
        cache = live.store_dir() / "s-aaa111.json"
        assert cache.is_file()
        registry.remove("s-aaa111", force=True)
        live.tick_once()
        assert live.sessions() == ["s-bbb222"]
        assert live.session_usage("s-aaa111") is None and not cache.exists()


class TestContainment:
    def test_a_planted_link_to_another_run_is_not_counted(self, tmp_path):
        _plant("s-victim1")
        attacker = transcripts.session_transcript_dir("s-attack1") / "-workspace"
        attacker.mkdir(parents=True)
        registry.register("s-attack1", pid=DEAD_PID, run={**RUN, "slug": "other"}, task={"harness": "claude"})
        victim_file = transcripts.session_transcript_dir("s-victim1") / "-workspace" / "main.jsonl"
        (attacker / "stolen.jsonl").symlink_to(victim_file)
        live = _live()
        live.tick_once()
        stolen = live.session_usage("s-attack1")
        assert stolen["totals"]["requests"] == 0
        assert stolen["status"] == "not_collected"


class TestHeadroomSamples:
    def test_samples_group_by_harness_and_the_host_side_operator(self):
        entries = [
            {"id": "s-1", "live": True, "task": {"harness": "claude", "operator": "alice"}},
            {"id": "s-2", "live": True, "task": {"harness": "codex", "operator": "alice"}},
        ]
        statuses = {
            "s-1": {"harness": "claude", "updated": "2026-11-05T10:00:00Z",
                    "limits": [{"key": "five_hour", "percent": 42, "resets_at": "2026-11-05T12:00:00Z"}],
                    "operator": "mallory"},
            "s-2": {"harness": "codex", "updated": "2026-11-05T10:00:00Z",
                    "codex": {"account": {"limits": [{"key": "primary", "percent": 15.0,
                                                      "resets_at": "2026-11-09T00:00:00Z"}]}}},
        }
        live = _live(sessions=lambda: entries, sources=lambda sid: [],
                     statuses=lambda e: statuses)
        live.tick_once()
        samples = live.samples()
        # The container-written document's own "operator" is ignored.
        assert set(samples) == {("claude", "alice", "five_hour"), ("codex", "alice", "primary")}
        assert samples[("claude", "alice", "five_hour")][0]["percent"] == 42.0


class TestNeverRaisesNeverWrites:
    def test_tick_survives_failing_seams(self):
        def boom(*a, **k):
            raise RuntimeError("seam failed")

        live = _live(sessions=boom)
        assert live.tick_once() == {"sessions": 0, "changed": 0}
        assert live.last_error == "seam failed"
        live = _live(sessions=lambda: [{"id": "s-x1", "live": True}], sources=boom, statuses=boom)
        live.tick_once()
        assert live.session_usage("s-x1")["status"] == "not_collected"

    def test_the_collector_has_no_work_repo_write_path(self):
        source = Path(usage_live.__file__).read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("workrepo", "work_repo", "git_ops", "subprocess"):
            assert not any(forbidden in name for name in imported), forbidden

    def test_a_tick_writes_only_under_the_platform_store(self, tmp_path):
        _plant("s-aaa111")
        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        _live().tick_once()
        written = {p for p in tmp_path.rglob("*") if p.is_file()} - before
        store_dir = store.platform_dir() / usage_live.STORE_DIRNAME
        assert written and all(p.parent == store_dir for p in written)


class TestDaemonWiring:
    def test_the_collector_starts_and_registers_when_enabled(self, monkeypatch):
        started = []
        monkeypatch.setattr(usage_live.UsageLive, "start", lambda self: started.append(self))
        live = daemon._collect_usage(_config())
        assert started == [live] and usage_live.current() is live
        usage_live.register(None)

    def test_the_collector_stays_off_when_disabled(self, monkeypatch):
        started = []
        monkeypatch.setattr(usage_live.UsageLive, "start", lambda self: started.append(self))
        daemon._collect_usage(_config(usage_enabled=False))
        assert started == [] and usage_live.current() is None

    @pytest.mark.parametrize("value,enabled", [("0", False), ("no", False), ("1", True)])
    def test_the_env_switch_reaches_the_config(self, monkeypatch, value, enabled):
        monkeypatch.setenv(cfg.ENV_USAGE, value)
        assert cfg.load().usage_enabled is enabled

    def test_interval_and_projection_come_from_the_env(self, monkeypatch):
        monkeypatch.setenv(cfg.ENV_USAGE_INTERVAL, "45")
        monkeypatch.setenv(cfg.ENV_USAGE_PROJECTION_MINUTES, "20")
        monkeypatch.setenv(cfg.ENV_USAGE_PRICES, "/etc/lmer/prices.yaml")
        loaded = cfg.load()
        assert (loaded.usage_interval_seconds, loaded.usage_projection_minutes, loaded.usage_prices_path) == (
            45, 20, "/etc/lmer/prices.yaml",
        )

    def test_a_non_positive_interval_is_refused(self, monkeypatch):
        monkeypatch.setenv(cfg.ENV_USAGE_INTERVAL, "0")
        with pytest.raises(cfg.ConfigError):
            cfg.load()


class TestSpawnRecordsTheOperator:
    def test_the_registry_task_block_carries_the_launch_operator(self):
        source = Path(spawn.__file__).read_text()
        assert '"operator": _session_metadata(request).get("operator")' in source
        request = SimpleNamespace(effort=None, session_metadata={"operator": "alice"}, harness=None,
                                  model=None, preset=None, agents=None, slot=None)
        assert spawn._session_metadata(request)["operator"] == "alice"


def test_a_fan_out_childs_transcript_is_not_the_parents_live_usage():
    """Children keep their transcripts in the parent's dir (#647 Q6), and the
    collector reads what the transcript view reads, so they are excluded; the
    run's record is where a child's usage is counted."""
    directory = _plant("s-kid111")
    child = directory.parent / ".lmer-children" / ("a" * 32) / "-workspace"
    child.mkdir(parents=True)
    # Ids distinct from the parent's, so reading it would change the count.
    shutil.copy(FIXTURE.parent.parent / "children" / "claude" / "main.jsonl", child / "c.jsonl")
    live = _live()
    live.tick_once()
    assert live.session_usage("s-kid111")["totals"]["requests"] == 7
