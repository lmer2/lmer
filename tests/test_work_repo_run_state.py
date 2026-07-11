"""Tests for the durable run-state kernel (work_repo.run_state)."""
import importlib.machinery
import importlib.util
import json
import os
import types
from pathlib import Path

import pytest

from work_repo import run_state
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so the host's real env can't leak in."""
    strip_lmer_env(monkeypatch)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """Set the full env trio + task context; return the expected run dir base."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo/-/issues/123")
    monkeypatch.setenv("LMER_SESSION_ID", "s-test-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs"


class TestDeriveSlug:
    def test_issue_url(self, run_env):
        assert run_state.derive_slug() == "develop-issue-123"

    def test_mr_url(self, run_env, monkeypatch):
        monkeypatch.setenv("LMER_TASK", "review")
        monkeypatch.setenv(
            "LMER_TASK_TARGET", "https://git.example.com/org/repo/-/merge_requests/456"
        )
        assert run_state.derive_slug() == "review-mr-456"

    def test_work_items_url_normalizes_to_issue(self, run_env, monkeypatch):
        monkeypatch.setenv(
            "LMER_TASK_TARGET", "https://git.example.com/org/repo/-/work_items/70"
        )
        assert run_state.derive_slug() == "develop-issue-70"

    def test_branch_target(self, run_env, monkeypatch):
        monkeypatch.setenv("LMER_TASK_TARGET", "feature/foo bar")
        assert run_state.derive_slug() == "develop-feature-foo-bar"

    def test_no_target_is_bare_taskdef(self, run_env, monkeypatch):
        monkeypatch.setenv("LMER_TASK", "chat")
        monkeypatch.delenv("LMER_TASK_TARGET")
        assert run_state.derive_slug() == "chat"

    def test_explicit_args_override_env(self, run_env):
        assert run_state.derive_slug("review", "https://x/org/p/pull/9") == "review-pr-9"


class TestPaths:
    def test_runs_base(self, run_env):
        assert run_state.runs_base() == run_env

    def test_runs_base_none_without_project(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        assert run_state.runs_base() is None

    def test_run_dir(self, run_env):
        assert run_state.run_dir() == run_env / "develop-issue-123"

    def test_run_dir_none_without_context(self):
        assert run_state.run_dir() is None

    def test_run_rel_path(self, run_env):
        assert run_state.run_rel_path() == "git.example.com/org/repo/runs/develop-issue-123"

    def test_run_rel_path_none_without_context(self):
        assert run_state.run_rel_path() is None


class TestSessionId:
    def test_from_env(self, run_env):
        assert run_state.current_session_id() == "s-test-1"

    def test_unknown_when_unset(self):
        assert run_state.current_session_id() == "unknown"


class TestStateIO:
    def test_seed_shape(self, run_env):
        state = run_state.seed_state("develop-issue-123", "develop", "https://x/-/issues/123")
        assert state["schema"] == run_state.SCHEMA_VERSION
        assert state["slug"] == "develop-issue-123"
        assert state["name"] is None
        assert state["taskdef"] == "develop"
        assert state["target"] == "https://x/-/issues/123"
        assert state["status"] == "in-progress"
        assert state["phase"] is None
        assert state["stop_reason"] is None
        assert state["critical_error"] is None
        assert state["goal"] is None
        assert state["artifacts"] == {}
        assert state["owner"] is None
        assert state["created"].endswith("Z")
        assert state["updated"] == state["created"]

    def test_write_then_load_roundtrip(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        state = run_state.seed_state("s", "develop", "t")
        run_state.write_state(rdir, state)
        loaded = run_state.load_state(rdir)
        assert loaded["slug"] == "s"
        assert not list(rdir.glob(".state.yaml.tmp"))  # no tmp file left behind

    def test_write_bumps_updated(self, run_env, tmp_path, monkeypatch):
        rdir = tmp_path / "r"
        state = run_state.seed_state("s", "develop", "t")
        monkeypatch.setattr(run_state, "utc_now_iso", lambda: "2027-01-01T00:00:00Z")
        run_state.write_state(rdir, state)
        assert run_state.load_state(rdir)["updated"] == "2027-01-01T00:00:00Z"

    def test_load_missing_returns_none(self, run_env, tmp_path):
        assert run_state.load_state(tmp_path / "nowhere") is None

    def test_load_corrupt_backs_up_and_raises(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yaml").write_text("{ this is: not: valid yaml [")
        with pytest.raises(run_state.RunStateError):
            run_state.load_state(rdir)
        assert not (rdir / "state.yaml").exists()
        assert list(rdir.glob("state.yaml.bad-*")), "corrupt file must be backed up"

    def test_load_non_dict_backs_up_and_raises(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yaml").write_text("- just\n- a list\n")
        with pytest.raises(run_state.RunStateError):
            run_state.load_state(rdir)
        assert list(rdir.glob("state.yaml.bad-*"))

    def test_load_schema_too_new_raises_without_touching_file(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yaml").write_text("schema: 999\nslug: s\n")
        with pytest.raises(run_state.RunStateError, match="read-only"):
            run_state.load_state(rdir)
        assert (rdir / "state.yaml").exists()  # too-new is refusal, not corruption

    def test_load_non_numeric_schema_backs_up_and_raises(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yaml").write_text("schema: abc\nslug: s\n")
        with pytest.raises(run_state.RunStateError):
            run_state.load_state(rdir)
        assert list(rdir.glob("state.yaml.bad-*"))


class TestLegacyStateMigration:
    """Lazy migration of pre-rename runs: state.yml is read when state.yaml
    is absent, and the first write moves it aside as state.yml.migrated."""

    def test_mutation_writes_yaml_not_yml(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        run_state.write_state(rdir, run_state.seed_state("s", "develop", "t"))
        assert (rdir / "state.yaml").exists()
        assert not (rdir / "state.yml").exists()

    def test_load_falls_back_to_legacy_yml(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yml").write_text("schema: 1\nslug: legacy-run\n")
        assert run_state.load_state(rdir)["slug"] == "legacy-run"

    def test_yaml_wins_over_leftover_legacy(self, run_env, tmp_path):
        # A crash between writing state.yaml and renaming state.yml must
        # never make the stale legacy contents readable again.
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yml").write_text("schema: 1\nslug: stale\n")
        (rdir / "state.yaml").write_text("schema: 1\nslug: current\n")
        assert run_state.load_state(rdir)["slug"] == "current"

    def test_first_write_migrates_legacy_aside(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yml").write_text("schema: 1\nslug: legacy-run\n")
        state = run_state.load_state(rdir)
        run_state.write_state(rdir, state)
        assert not (rdir / "state.yml").exists()
        assert (rdir / "state.yml.migrated").exists()
        assert run_state.load_state(rdir)["slug"] == "legacy-run"

    def test_corrupt_backup_named_after_file_actually_read(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        (rdir / "state.yml").write_text("{ this is: not: valid yaml [")
        with pytest.raises(run_state.RunStateError, match="state.yml"):
            run_state.load_state(rdir)
        assert not (rdir / "state.yml").exists()
        assert list(rdir.glob("state.yml.bad-*"))
        assert not list(rdir.glob("state.yaml.bad-*"))


class TestEvents:
    def test_append_and_read(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        run_state.append_event(rdir, "run_seeded")
        run_state.append_event(rdir, "phase", note="interview")
        run_state.append_event(rdir, "gate", note="gate-check: pass", data={"rc": 0})
        events = run_state.read_events(rdir, last_n=0)
        assert [e["type"] for e in events] == ["run_seeded", "phase", "gate"]
        assert events[0]["session"] == "s-test-1"
        assert events[0]["ts"].endswith("Z")
        assert events[1]["note"] == "interview"
        assert events[2]["data"] == {"rc": 0}
        assert "note" not in events[0]  # optional fields omitted when unset

    def test_read_last_n(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        for i in range(10):
            run_state.append_event(rdir, f"t{i}")
        events = run_state.read_events(rdir, last_n=3)
        assert [e["type"] for e in events] == ["t7", "t8", "t9"]

    def test_read_missing_file(self, run_env, tmp_path):
        assert run_state.read_events(tmp_path / "nowhere") == []

    def test_torn_line_is_skipped(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        run_state.append_event(rdir, "good")
        with open(rdir / "events.jsonl", "a") as fh:
            fh.write('{"ts": "torn...\n')
        run_state.append_event(rdir, "also_good")
        events = run_state.read_events(rdir, last_n=0)
        assert [e["type"] for e in events] == ["good", "also_good"]

    def test_empty_but_set_optional_fields_persisted(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        run_state.append_event(rdir, "t", note="", data={})
        event = run_state.read_events(rdir, last_n=0)[0]
        assert event["note"] == ""
        assert event["data"] == {}


def _state(**overrides):
    base = run_state.seed_state("develop-issue-123", "develop", "https://x/-/issues/123")
    base.update(overrides)
    return base


class TestDecide:
    def test_no_state(self):
        d = run_state.decide(None, [], "s-1")
        assert d == {"kind": "none"}

    def test_in_progress_question(self):
        state = _state(phase="interview", stop_reason="question", goal="align approach")
        events = [{"ts": "2026-07-03T10:00:00Z", "session": "s-0", "type": "phase", "note": "interview"}]
        d = run_state.decide(state, events, "s-1")
        assert d["kind"] == "run"
        assert d["phase"] == "interview"
        assert d["stop_reason"] == "question"
        assert d["goal"] == "align approach"
        assert d["recent_events"] == events
        assert d["warnings"] == []

    def test_completed_run(self):
        d = run_state.decide(_state(status="complete", stop_reason="complete"), [], "s-1")
        assert d["status"] == "complete"

    def test_critical_error_carried(self):
        state = _state(stop_reason="critical_error",
                       critical_error={"summary": "boom", "detail": "trace"})
        d = run_state.decide(state, [], "s-1")
        assert d["critical_error"]["summary"] == "boom"

    def test_fresh_foreign_claim_warns_live(self):
        state = _state(owner={"session_id": "s-other", "claimed_at": "2026-07-03T11:30:00Z"})
        d = run_state.decide(state, [], "s-1", now="2026-07-03T12:00:00Z")
        assert any("another live session" in w for w in d["warnings"])

    def test_stale_foreign_claim_warns_stale(self):
        state = _state(owner={"session_id": "s-other", "claimed_at": "2026-07-03T01:00:00Z"})
        d = run_state.decide(state, [], "s-1", now="2026-07-03T12:00:00Z")
        assert any("stale" in w for w in d["warnings"])

    def test_own_claim_no_warning(self):
        state = _state(owner={"session_id": "s-1", "claimed_at": "2026-07-03T11:59:00Z"})
        d = run_state.decide(state, [], "s-1", now="2026-07-03T12:00:00Z")
        assert d["warnings"] == []

    def test_name_passed_through(self):
        d = run_state.decide(_state(name="hunt the seg fault"), [], "s-1")
        assert d["name"] == "hunt the seg fault"

    def test_unset_name_passed_through_as_none(self):
        d = run_state.decide(_state(), [], "s-1")
        assert d["name"] is None

    def test_legacy_state_without_name_key(self):
        state = _state()
        del state["name"]  # pre-name state.yaml files lack the key entirely
        d = run_state.decide(state, [], "s-1")
        assert d["name"] is None

    def test_default_now_fallback_classifies_fresh_claim(self):
        state = _state(owner={"session_id": "s-other",
                              "claimed_at": run_state.utc_now_iso()})
        d = run_state.decide(state, [], "s-1")
        assert any("another live session" in w for w in d["warnings"])


class TestFormatBrief:
    def test_none_brief(self):
        text = run_state.format_brief({"kind": "none"})
        assert "fresh run" in text.lower()

    def test_run_brief_mentions_key_fields(self):
        state = _state(phase="interview", stop_reason="question", goal="align",
                       artifacts={"spec": "spec.md"})
        events = [{"ts": "2026-07-03T10:00:00Z", "session": "s-0",
                   "type": "phase", "note": "interview"}]
        d = run_state.decide(state, events, "s-1")
        text = run_state.format_brief(d)
        assert "develop-issue-123" in text
        assert "interview" in text
        assert "question" in text
        assert "spec.md" in text
        assert "align" in text

    def test_brief_header_without_name_unchanged(self):
        d = run_state.decide(_state(), [], "s-1")
        text = run_state.format_brief(d)
        assert text.splitlines()[0] == "Run: develop-issue-123 (status: in-progress)"

    def test_brief_header_renders_name_first_when_set(self):
        d = run_state.decide(_state(name="hunt the seg fault"), [], "s-1")
        text = run_state.format_brief(d)
        assert text.splitlines()[0] == (
            "Run: hunt the seg fault (slug: develop-issue-123, status: in-progress)"
        )

    def test_brief_tolerates_non_dict_critical_error(self):
        state = _state(stop_reason="critical_error", critical_error="oops")
        d = run_state.decide(state, [], "s-1")
        text = run_state.format_brief(d)
        assert "oops" in text


class TestEmitGateEvent:
    def test_emits_when_run_exists(self, run_env, tmp_path):
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        run_state.emit_gate_event("gate-check", "pass")
        events = run_state.read_events(rdir, last_n=0)
        assert events[-1]["type"] == "gate"
        assert events[-1]["note"] == "gate-check: pass"

    def test_silent_without_run(self, run_env, capsys):
        run_state.emit_gate_event("gate-check", "fail")  # no state.yaml seeded
        rdir = run_state.run_dir()
        assert not (rdir / "events.jsonl").exists()
        assert capsys.readouterr().out == ""

    def test_silent_without_context(self, capsys):
        run_state.emit_gate_event("gate-push", "pass")  # no env at all
        assert capsys.readouterr().out == ""

    def test_never_raises(self, run_env, monkeypatch):
        monkeypatch.setattr(run_state, "append_event",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("s", "develop", "t"))
        run_state.emit_gate_event("gate-commit", "bypass")  # must not raise

    def test_minimal_receipt_payload(self, run_env):
        """gate/outcome are always in data; unmeasured fields stay absent."""
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        run_state.emit_gate_event("gate-check", "fail")
        event = run_state.read_events(rdir, last_n=0)[-1]
        assert event["note"] == "gate-check: fail"
        assert event["data"] == {"gate": "gate-check", "outcome": "fail"}

    def test_full_receipt_payload(self, run_env):
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        run_state.emit_gate_event(
            "gate-commit", "pass",
            exit_code=0,
            duration_s=12.34,
            summary="1397 passed in 41.8s",
            argv=["gate-commit", "-m", "msg"],
            commit_sha="a" * 40,
        )
        event = run_state.read_events(rdir, last_n=0)[-1]
        assert event["note"] == "gate-commit: pass"
        assert event["data"] == {
            "gate": "gate-commit",
            "outcome": "pass",
            "exit_code": 0,
            "duration_s": 12.3,  # rounded to one decimal
            "summary": "1397 passed in 41.8s",
            "argv": ["gate-commit", "-m", "msg"],
            "commit_sha": "a" * 40,
        }

    def test_exit_code_zero_is_recorded(self, run_env):
        """0 is a value, not an absence — the receipt must record it."""
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        run_state.emit_gate_event("gate-push", "pass", exit_code=0)
        event = run_state.read_events(rdir, last_n=0)[-1]
        assert event["data"]["exit_code"] == 0

    def test_summary_and_argv_are_redacted(self, run_env, monkeypatch):
        """Receipt text lands in the shared work repo — it goes through
        redact_secrets like the other writers' output."""
        monkeypatch.setattr(run_state, "redact_secrets", lambda s: "<redacted>")
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        run_state.emit_gate_event(
            "gate-commit", "pass",
            summary="token=hunter2", argv=["gate-commit", "-m", "token=hunter2"],
        )
        data = run_state.read_events(rdir, last_n=0)[-1]["data"]
        assert data["summary"] == "<redacted>"
        assert data["argv"] == ["<redacted>"] * 3


def _make_bundle(rdir, mp_slug, *names):
    """Create masterplan/<mp_slug>/ under rdir holding the given artifacts."""
    bundle = rdir / "masterplan" / mp_slug
    bundle.mkdir(parents=True, exist_ok=True)
    for name in names:
        (bundle / name).write_text(f"{mp_slug}:{name}\n")
    return bundle


class TestSyncMasterplanArtifacts:
    def test_links_present_artifacts_only(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md", "plan.md")
        linked = run_state.sync_masterplan_artifacts(rdir)
        assert linked == ["spec.md", "plan.md"]
        assert (rdir / "spec.md").is_symlink()
        assert (rdir / "plan.md").is_symlink()
        for absent in ("goals.md", "plan.html", "retro.md"):
            assert not (rdir / absent).exists()

    def test_link_targets_are_relative_and_resolve(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md")
        run_state.sync_masterplan_artifacts(rdir)
        assert os.readlink(rdir / "spec.md") == "masterplan/mp-a/spec.md"
        assert (rdir / "spec.md").read_text() == "mp-a:spec.md\n"

    def test_single_bundle_uses_plain_names(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "retro.md")
        assert run_state.sync_masterplan_artifacts(rdir) == ["retro.md"]

    def test_multiple_bundles_use_slug_prefixed_names(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md")
        _make_bundle(rdir, "mp-b", "spec.md", "plan.html")
        linked = run_state.sync_masterplan_artifacts(rdir)
        assert linked == ["mp-a-spec.md", "mp-b-spec.md", "mp-b-plan.html"]
        assert os.readlink(rdir / "mp-b-plan.html") == "masterplan/mp-b/plan.html"
        assert not (rdir / "spec.md").exists()  # no plain names in multi mode

    def test_regular_file_replaced_by_symlink(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md")
        (rdir / "spec.md").write_text("manually copied, stale\n")
        run_state.sync_masterplan_artifacts(rdir)
        assert (rdir / "spec.md").is_symlink()
        assert (rdir / "spec.md").read_text() == "mp-a:spec.md\n"

    def test_wrong_symlink_repointed(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md")
        (rdir / "spec.md").symlink_to("somewhere/else.md")
        run_state.sync_masterplan_artifacts(rdir)
        assert os.readlink(rdir / "spec.md") == "masterplan/mp-a/spec.md"

    def test_existing_correct_symlink_left_alone(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md")
        assert run_state.sync_masterplan_artifacts(rdir) == ["spec.md"]
        before = os.lstat(rdir / "spec.md")
        assert run_state.sync_masterplan_artifacts(rdir) == ["spec.md"]
        after = os.lstat(rdir / "spec.md")
        assert (before.st_ino, before.st_dev) == (after.st_ino, after.st_dev)

    def test_registers_links_in_state_artifacts(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        run_state.write_state(rdir, run_state.seed_state("s", "develop", "t"))
        _make_bundle(rdir, "mp-a", "spec.md", "goals.md")
        run_state.sync_masterplan_artifacts(rdir)
        artifacts = run_state.load_state(rdir)["artifacts"]
        # Keyed by filename stem, matching `work artifact`'s convention.
        assert artifacts["spec"] == "spec.md"
        assert artifacts["goals"] == "goals.md"

    def test_registration_preserves_existing_artifacts(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        state = run_state.seed_state("s", "develop", "t")
        state["artifacts"] = {"notes": "notes.md"}
        run_state.write_state(rdir, state)
        _make_bundle(rdir, "mp-a", "spec.md")
        run_state.sync_masterplan_artifacts(rdir)
        artifacts = run_state.load_state(rdir)["artifacts"]
        assert artifacts == {"notes": "notes.md", "spec": "spec.md"}

    def test_plan_md_and_html_both_stay_registered(self, run_env, tmp_path, monkeypatch):
        # plan.md and plan.html share the stem `plan`: the collision must
        # not drop either from the registry, and the keys must stay stable
        # so a re-sync writes nothing (no flip-flopping `changed`).
        rdir = tmp_path / "r"
        run_state.write_state(rdir, run_state.seed_state("s", "develop", "t"))
        _make_bundle(rdir, "mp-a", "plan.md", "plan.html")
        run_state.sync_masterplan_artifacts(rdir)
        artifacts = run_state.load_state(rdir)["artifacts"]
        assert artifacts["plan"] == "plan.md"
        assert artifacts["plan.html"] == "plan.html"
        writes = []
        monkeypatch.setattr(
            run_state, "write_state",
            lambda *a, **k: writes.append(a),
        )
        run_state.sync_masterplan_artifacts(rdir)
        assert writes == []  # second sync: keys stable, nothing rewritten

    def test_second_sync_does_not_rewrite_state(self, run_env, tmp_path, monkeypatch):
        rdir = tmp_path / "r"
        run_state.write_state(rdir, run_state.seed_state("s", "develop", "t"))
        _make_bundle(rdir, "mp-a", "spec.md")
        run_state.sync_masterplan_artifacts(rdir)
        writes = []
        monkeypatch.setattr(
            run_state, "write_state",
            lambda *a, **k: writes.append(a),
        )
        assert run_state.sync_masterplan_artifacts(rdir) == ["spec.md"]
        assert writes == []  # idempotent: nothing changed, single writer not invoked

    def test_no_masterplan_dir_untouched(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        rdir.mkdir()
        assert run_state.sync_masterplan_artifacts(rdir) == []
        assert list(rdir.iterdir()) == []  # no links, no state write

    def test_missing_run_dir_returns_empty(self, run_env, tmp_path):
        assert run_state.sync_masterplan_artifacts(tmp_path / "nowhere") == []

    def test_empty_bundle_no_links_no_state_write(self, run_env, tmp_path):
        rdir = tmp_path / "r"
        (rdir / "masterplan" / "mp-a").mkdir(parents=True)
        assert run_state.sync_masterplan_artifacts(rdir) == []
        assert not (rdir / "state.yaml").exists()

    def test_links_created_without_state_file(self, run_env, tmp_path):
        # A run dir that was never seeded still gets its links; no state
        # file is invented for it.
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md")
        assert run_state.sync_masterplan_artifacts(rdir) == ["spec.md"]
        assert not (rdir / "state.yaml").exists()

    def test_corrupt_state_fail_soft(self, run_env, tmp_path, capsys):
        rdir = tmp_path / "r"
        _make_bundle(rdir, "mp-a", "spec.md")
        (rdir / "state.yaml").write_text("{ this is: not: valid yaml [")
        linked = run_state.sync_masterplan_artifacts(rdir)  # must not raise
        assert linked == ["spec.md"]
        assert (rdir / "spec.md").is_symlink()
        assert "masterplan artifact sync" in capsys.readouterr().out

    def test_symlink_failure_fail_soft(self, run_env, tmp_path, monkeypatch, capsys):
        rdir = tmp_path / "r"
        run_state.write_state(rdir, run_state.seed_state("s", "develop", "t"))
        _make_bundle(rdir, "mp-a", "spec.md")
        monkeypatch.setattr(
            Path, "symlink_to",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no symlinks here")),
        )
        assert run_state.sync_masterplan_artifacts(rdir) == []  # must not raise
        assert "skipped spec.md" in capsys.readouterr().out
        assert run_state.load_state(rdir)["artifacts"] == {}  # nothing registered


def _load_gate_bin(name):
    """Load a bin/gate-* script (extensionless) as an importable module."""
    path = Path(__file__).parent.parent / "bin" / name
    modname = name.replace("-", "_") + "_bin"
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _gate_events(rdir):
    return [e for e in run_state.read_events(rdir, last_n=0) if e["type"] == "gate"]


def _fake_git(sha="c0ffee12deadbeefc0ffee12deadbeefc0ffee12", commit_rc=0):
    """A subprocess.run stand-in covering gate-commit's two git calls:
    `git commit` (returncode only) and `git rev-parse HEAD` (stdout)."""
    def run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return types.SimpleNamespace(returncode=0, stdout=f"{sha}\n")
        return types.SimpleNamespace(returncode=commit_rc, stdout="")
    return run


class TestGateCommitReceipt:
    """bin/gate-commit emits exactly ONE receipt per invocation, at the end,
    carrying the whole-command exit code and — when a commit landed — the
    resulting sha (issue #88 D1)."""

    SHA = "c0ffee12deadbeefc0ffee12deadbeefc0ffee12"

    def _seeded_rdir(self):
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        return rdir

    def _patched(self, monkeypatch, gate_passes=True, commit_rc=0):
        mod = _load_gate_bin("gate-commit")
        monkeypatch.setattr(mod.subprocess, "run", _fake_git(self.SHA, commit_rc))
        monkeypatch.setattr(
            "lmer_cli.gates.GateSystem.run_commit_gate",
            lambda self, skip_tests=False: gate_passes,
        )
        return mod

    def test_pass_receipt_carries_sha(self, run_env, monkeypatch):
        rdir = self._seeded_rdir()
        mod = self._patched(monkeypatch, gate_passes=True, commit_rc=0)
        assert mod.gate_commit("safe message") == 0
        events = _gate_events(rdir)
        assert [e["note"] for e in events] == ["gate-commit: pass"]
        data = events[0]["data"]
        assert data["outcome"] == "pass"
        assert data["exit_code"] == 0
        assert data["commit_sha"] == self.SHA
        assert data["duration_s"] >= 0
        assert data["argv"]  # invocation recorded (pytest's argv here)
        assert "summary" not in data  # no checks captured output — not fabricated

    def test_bypass_receipt_carries_sha(self, run_env, monkeypatch):
        rdir = self._seeded_rdir()
        mod = self._patched(monkeypatch, commit_rc=0)
        assert mod.gate_commit("safe message", bypass=True) == 0
        events = _gate_events(rdir)
        assert [e["note"] for e in events] == ["gate-commit: bypass"]
        data = events[0]["data"]
        assert data["outcome"] == "bypass"
        assert data["exit_code"] == 0
        assert data["commit_sha"] == self.SHA
        assert "summary" not in data  # nothing ran — nothing to summarize

    def test_fail_receipt_has_no_sha(self, run_env, monkeypatch):
        rdir = self._seeded_rdir()
        mod = self._patched(monkeypatch, gate_passes=False)
        assert mod.gate_commit("safe message") == 1
        events = _gate_events(rdir)
        assert [e["note"] for e in events] == ["gate-commit: fail"]
        data = events[0]["data"]
        assert data["exit_code"] == 1
        assert "commit_sha" not in data

    def test_attribution_block_is_pass_with_exit_one(self, run_env, monkeypatch):
        rdir = self._seeded_rdir()
        mod = self._patched(monkeypatch, gate_passes=True)
        assert mod.gate_commit("done\n\nCo-Authored-By: Claude <noreply@anthropic.com>") == 1
        events = _gate_events(rdir)
        assert [e["note"] for e in events] == ["gate-commit: pass"]
        data = events[0]["data"]
        assert data["exit_code"] == 1
        assert "commit_sha" not in data  # nothing was committed

    def test_commit_failure_is_pass_with_commit_exit_code(self, run_env, monkeypatch):
        rdir = self._seeded_rdir()
        mod = self._patched(monkeypatch, gate_passes=True, commit_rc=128)
        assert mod.gate_commit("safe message") == 128
        data = _gate_events(rdir)[0]["data"]
        assert data["outcome"] == "pass"
        assert data["exit_code"] == 128
        assert "commit_sha" not in data

    def test_receipt_assembly_failure_never_changes_exit_code(self, run_env, monkeypatch):
        """Fail-soft covers ASSEMBLY too: a summary/sha computation that
        blows up must not turn a successful commit into a failure."""
        self._seeded_rdir()
        mod = self._patched(monkeypatch, gate_passes=True, commit_rc=0)
        monkeypatch.setattr(
            "lmer_cli.gates.GateSystem.receipt_summary",
            lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert mod.gate_commit("safe message") == 0

    def test_head_sha_failure_never_changes_exit_code(self, run_env, monkeypatch):
        self._seeded_rdir()
        mod = self._patched(monkeypatch, gate_passes=True, commit_rc=0)
        monkeypatch.setattr(
            mod, "_head_sha",
            lambda: (_ for _ in ()).throw(OSError("fork failed")),
        )
        assert mod.gate_commit("safe message") == 0


class TestGateCheckReceipt:
    def test_pass_receipt(self, run_env, monkeypatch):
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        mod = _load_gate_bin("gate-check")
        monkeypatch.setattr(mod, "commit_gate", lambda *a, **k: 0)
        monkeypatch.setattr(mod.sys, "argv", ["gate-check"])
        assert mod.main() == 0
        events = _gate_events(rdir)
        assert [e["note"] for e in events] == ["gate-check: pass"]
        data = events[0]["data"]
        assert data == {
            "gate": "gate-check", "outcome": "pass", "exit_code": 0,
            "duration_s": data["duration_s"], "argv": ["gate-check"],
        }

    def test_fail_receipt(self, run_env, monkeypatch):
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        mod = _load_gate_bin("gate-check")
        monkeypatch.setattr(mod, "commit_gate", lambda *a, **k: 1)
        monkeypatch.setattr(mod.sys, "argv", ["gate-check", "-v"])
        assert mod.main() == 1
        data = _gate_events(rdir)[0]["data"]
        assert data["outcome"] == "fail"
        assert data["exit_code"] == 1
        assert data["argv"] == ["gate-check", "-v"]

    def test_receipt_assembly_failure_never_changes_exit_code(self, run_env, monkeypatch):
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        mod = _load_gate_bin("gate-check")
        monkeypatch.setattr(mod, "commit_gate", lambda *a, **k: 0)
        monkeypatch.setattr(mod.sys, "argv", ["gate-check"])
        monkeypatch.setattr(
            "lmer_cli.gates.GateSystem.receipt_summary",
            lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert mod.main() == 0


class TestGatePushReceipt:
    def _seeded(self):
        rdir = run_state.run_dir()
        run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
        return rdir

    def _patched(self, monkeypatch, checks_rc=0, push_rc=0, branch="feature/x"):
        mod = _load_gate_bin("gate-push")
        monkeypatch.setattr(mod, "push_gate", lambda *a, **k: checks_rc)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "branch"]:
                return types.SimpleNamespace(returncode=0, stdout=f"{branch}\n")
            return types.SimpleNamespace(returncode=push_rc, stdout="")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        return mod

    def test_pass_receipt(self, run_env, monkeypatch):
        rdir = self._seeded()
        mod = self._patched(monkeypatch)
        assert mod.gate_push() == 0
        events = _gate_events(rdir)
        assert [e["note"] for e in events] == ["gate-push: pass"]
        assert events[0]["data"]["exit_code"] == 0

    def test_checks_fail_receipt(self, run_env, monkeypatch):
        rdir = self._seeded()
        mod = self._patched(monkeypatch, checks_rc=1)
        assert mod.gate_push() == 1
        data = _gate_events(rdir)[0]["data"]
        assert data["outcome"] == "fail"
        assert data["exit_code"] == 1

    def test_push_failure_is_pass_with_push_exit_code(self, run_env, monkeypatch):
        rdir = self._seeded()
        mod = self._patched(monkeypatch, push_rc=1)
        assert mod.gate_push() == 1
        data = _gate_events(rdir)[0]["data"]
        assert data["outcome"] == "pass"
        assert data["exit_code"] == 1

    def test_no_branch_is_pass_with_exit_one(self, run_env, monkeypatch):
        rdir = self._seeded()
        mod = self._patched(monkeypatch, branch="")
        assert mod.gate_push() == 1
        data = _gate_events(rdir)[0]["data"]
        assert data["outcome"] == "pass"
        assert data["exit_code"] == 1
