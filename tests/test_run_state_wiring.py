"""Source-level wiring guards for the run-state layer.

The runner and settings.json are exercised for real only inside the
container; these guards pin the wiring so a refactor can't silently drop
it (same pattern as the source-guard test in tests/test_start_hook.py).
"""
import inspect
import json
import re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

REPO_ROOT = Path(__file__).parent.parent


class TestSessionIdMinting:
    def test_runner_exports_session_id(self):
        runner = (REPO_ROOT / "libexec" / "claude-runner.sh").read_text()
        assert re.search(r'export LMER_SESSION_ID=', runner), \
            "claude-runner.sh must mint/export LMER_SESSION_ID"
        assert 'LMER_SESSION_ID:-' in runner, \
            "a host-injected LMER_SESSION_ID must be preserved"


class TestSessionEndHook:
    def test_settings_has_session_end_hook(self):
        settings = json.loads(
            (REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text()
        )
        session_end = settings["hooks"]["SessionEnd"]
        commands = [
            h["command"]
            for entry in session_end
            for h in entry["hooks"]
            if h.get("type") == "command"
        ]
        assert any("work session-end" in c for c in commands)
        # Fail-soft: hook must not error when `work` is absent (host sessions).
        assert any("command -v work" in c for c in commands)

    def test_existing_stop_hook_untouched(self):
        settings = json.loads(
            (REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text()
        )
        stop_commands = [
            h["command"]
            for entry in settings["hooks"]["Stop"]
            for h in entry["hooks"]
        ]
        assert any("slack_reply_guard.py" in c for c in stop_commands)


class TestRunStateFragment:
    def _render(self, monkeypatch, context):
        env = Environment(loader=FileSystemLoader(str(REPO_ROOT / "taskdef")))
        return env.get_template("run-state.jinja2").render(**context)

    def test_renders_brief_and_contract(self, monkeypatch):
        out = self._render(monkeypatch, {
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "org/repo",
            "run_state_brief": "Run: develop-issue-123 (status: in-progress)",
        })
        assert "develop-issue-123" in out
        assert "work state set --phase=" in out
        assert "stop-reason=question" in out
        assert "work artifact" in out
        # Receipts contract (issue #88 D3): validation runs through the
        # tools, plans name their validation, claims need receipts.
        assert "work verify" in out
        assert "verify:" in out
        assert "receipt" in out.lower()
        # Ledger contract (issue #89): gate-commit then ledger in the same
        # breath, ledger.yaml single-writer taught up front.
        assert "work ledger set <task-id> --status done --commit <sha>" in out
        assert "ledger.yaml" in out
        # Plan-gate lint (issue #90): the index is authored beside plan.md
        # and the gate presents plan.md only with a green check included.
        assert "plan.index.json" in out
        assert "work plan check" in out
        assert "green" in out.lower()
        assert "shared_files" in out

    def test_empty_without_project_context(self):
        out = self._render(None, {})
        assert out.strip() == ""

    def test_no_brief_section_when_fresh(self):
        out = self._render(None, {
            "LMER_REPO_HOST": "h", "LMER_REPO_PROJECT": "p",
        })
        assert "work state set" in out             # contract still taught
        assert "read this first" not in out.lower()  # no brief block rendered

    def test_run_naming_needs_no_confirmation(self):
        """Issue #94: the agent names the run itself — the fragment must
        teach `work name` directly, never routed through AskUserQuestion
        (the old propose-and-default-accept flow must not return)."""
        out = self._render(None, {
            "LMER_REPO_HOST": "h", "LMER_REPO_PROJECT": "p",
        })
        assert "work name" in out
        assert "DEFAULTS TO ACCEPTED" not in out
        name_bullet = re.search(r"- \*\*Name the run.*?(?=\n- |\Z)", out, re.DOTALL)
        assert name_bullet, "the Name-the-run ground rule went missing"
        # The task-direction rule legitimately keeps AskUserQuestion; the
        # naming bullet must not ask anything.
        assert "AskUserQuestion" not in name_bullet.group(0)
        assert "confirm" in name_bullet.group(0).lower()


class TestChatInclude:
    def test_chat_includes_fragment(self):
        chat = (REPO_ROOT / "taskdef" / "chat" / "instructions.txt").read_text()
        assert "{% include 'run-state.jinja2' ignore missing %}" in chat


class TestGateReceiptStubs:
    """The gate bins import emit_gate_event behind an ImportError stub so
    run-state wiring can never break a gate (issue #88 D4). The stub must
    swallow the receipt keyword arguments too, or a broken import would
    surface as a TypeError at the enriched call sites."""

    GATE_BINS = ("gate-check", "gate-commit", "gate-push")

    def test_bins_keep_import_fallback_stub(self):
        for name in self.GATE_BINS:
            text = (REPO_ROOT / "bin" / name).read_text()
            assert "from work_repo.run_state import emit_gate_event" in text, \
                f"{name}: emit_gate_event import missing"
            assert "except ImportError" in text, \
                f"{name}: ImportError fallback missing"
            assert re.search(
                r"def emit_gate_event\(gate, outcome, \*\*kwargs\):", text
            ), f"{name}: stub must accept the receipt kwargs"

    def test_real_signature_accepts_receipt_kwargs(self):
        """The kwargs the bins pass must exist on the real function — else
        the receipt path works with the stub and dies with the import."""
        from work_repo.run_state import emit_gate_event
        accepted = set(inspect.signature(emit_gate_event).parameters)
        assert {"exit_code", "duration_s", "summary", "argv", "commit_sha"} <= accepted

    def test_bins_pass_receipt_fields(self):
        for name in self.GATE_BINS:
            text = (REPO_ROOT / "bin" / name).read_text()
            for field in ("exit_code=", "duration_s=", "summary=", "argv="):
                assert field in text, f"{name}: receipt field {field} dropped"
        assert "commit_sha=" in (REPO_ROOT / "bin" / "gate-commit").read_text()


class TestAgentBriefs:
    AGENTS_DIR = REPO_ROOT / "agent-files" / "claude" / "agents"

    def test_briefs_exist_with_frontmatter(self):
        for name in ("explorer.md", "adversarial-reviewer.md"):
            text = (self.AGENTS_DIR / name).read_text()
            assert text.startswith("---\n"), f"{name}: missing frontmatter"
            frontmatter = text.split("---")[1]
            assert f"name: {name.removesuffix('.md')}" in frontmatter
            assert "description:" in frontmatter
            assert "tools: Read, Grep, Glob, Bash" in frontmatter, f"{name}: tools line changed"
            assert "Write" not in frontmatter, f"{name}: Write must not be granted"

    def test_explorer_is_read_only(self):
        text = (self.AGENTS_DIR / "explorer.md").read_text()
        assert "READ-ONLY" in text
        assert "Write" not in text.split("---")[1]  # no Write tool in frontmatter

    def test_model_pins(self):
        # Recon is mechanical: explorer is pinned to a cheaper tier. The
        # adversarial reviewer must NOT be pinned — judgment work inherits
        # the session model.
        explorer_fm = (self.AGENTS_DIR / "explorer.md").read_text().split("---")[1]
        assert "model: sonnet" in explorer_fm
        reviewer_fm = (self.AGENTS_DIR / "adversarial-reviewer.md").read_text().split("---")[1]
        assert "model:" not in reviewer_fm, "reviewer inherits the session model"

    def test_reviewer_carries_review_doctrine(self):
        text = (self.AGENTS_DIR / "adversarial-reviewer.md").read_text().lower()
        for marker in ("scope audit", "semantics over mechanism",
                       "re-examine framing", "carry intent",
                       "missing tests", "do not flag", "inconclusive"):
            assert marker in text, f"doctrine marker missing: {marker}"


class TestDangerZoneSettingsPreserveHooks:
    """entrypoint.sh's danger-zone settings rewrite must not drop hooks.

    Regression guard for the A4 finding: '{statusLine} + $perms' produced a
    settings.json with no hooks at all, silently disabling the SessionEnd
    run-state release and the Stop slack guard in danger-zone sessions.
    """

    def test_danger_zone_jq_merges_full_settings(self):
        entrypoint = (REPO_ROOT / "Ctl" / "container" / "entrypoint.sh").read_text()
        assert "'. + $perms'" in entrypoint, \
            "danger-zone jq must shallow-merge over the FULL settings"
        assert "'{statusLine} + $perms'" not in entrypoint, \
            "hook-dropping settings shape must not return"

    def test_danger_zone_merge_behavior(self, tmp_path):
        import shutil as _shutil
        import subprocess as _subprocess
        if not _shutil.which("jq"):
            import pytest as _pytest
            _pytest.skip("jq not available on host")
        src = REPO_ROOT / "agent-files" / "claude" / "settings.json"
        perms = ('{"skipDangerousModePermissionPrompt": true,'
                 '"skipAutoPermissionPrompt": true,'
                 '"permissions": {"defaultMode": "bypassPermissions"}}')
        out = _subprocess.run(
            ["jq", "--argjson", "perms", perms, ". + $perms", str(src)],
            capture_output=True, text=True, check=True,
        )
        merged = json.loads(out.stdout)
        assert "SessionEnd" in merged["hooks"], "SessionEnd hook must survive danger zone"
        assert "Stop" in merged["hooks"], "Stop hook must survive danger zone"
        assert merged["permissions"] == {"defaultMode": "bypassPermissions"}
        assert "statusLine" in merged


class TestHarnessSessionEndBackstop:
    """dispatch_runner must run the session-end backstop after the runner
    exits (Claude fires SessionEnd hooks without blocking exit on them, so
    the in-claude hook's push can be killed by container teardown)."""

    def test_dispatch_runner_runs_session_end_after_runner(self, monkeypatch):
        from lmer_cli.container import clone_and_exec
        calls = []

        class FakeProc:
            def wait(self):
                calls.append("runner-wait")
                return 7

        monkeypatch.setattr(
            clone_and_exec.subprocess, "Popen",
            lambda cmd: (calls.append(("popen", tuple(cmd))), FakeProc())[1],
        )
        monkeypatch.setattr(clone_and_exec, "_forward_signals", lambda proc: None)
        monkeypatch.setattr(
            clone_and_exec, "run_state_session_end",
            lambda: calls.append("session-end"),
        )
        rc = clone_and_exec.dispatch_runner("/bin/runner")
        assert rc == 7
        assert ("popen", ("/bin/runner",)) in calls
        assert calls.index("runner-wait") < calls.index("session-end")

    def test_backstop_fail_soft(self, monkeypatch):
        from lmer_cli.container import clone_and_exec
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda _: "/bin/work")

        def boom(*a, **k):
            raise OSError("teardown env gone")

        monkeypatch.setattr(clone_and_exec.subprocess, "call", boom)
        clone_and_exec.run_state_session_end()  # must not raise

    def test_backstop_skips_without_work_cli(self, monkeypatch):
        from lmer_cli.container import clone_and_exec
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda _: None)
        called = []
        monkeypatch.setattr(
            clone_and_exec.subprocess, "call",
            lambda *a, **k: called.append(a),
        )
        clone_and_exec.run_state_session_end()
        assert called == []
