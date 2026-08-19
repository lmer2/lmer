"""Tests for the orchestrator signal-reminder Stop hook (hooks/signal_guard.py)."""
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from hooks.signal_guard import (
    ANSWER_SUFFIX,
    CLOSED_SUFFIX,
    FACT_MILESTONE_KEY,
    FACT_MILESTONE_LABEL,
    MARKER_TEMPLATE,
    NUDGE_CAP,
    QUESTION_SUFFIX,
    SIGNAL_SUFFIX,
    _MILESTONE_PATTERNS,
    advanced_baseline,
    build_reason,
    env_flag,
    evaluate,
    fact_milestone,
    has_open_question,
    iter_messages,
    newest_open_question_id,
    newest_signal_id,
    parse_resume_json,
    signal_is_new,
    transcript_ends_signalled,
    transcript_has_signal,
    unsignalled_milestone,
    write_marker,
)

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "signal_guard.py"

# One representative hit and one near-miss per pattern family. Spellings are the
# real ones — `--review-file` posts a review on both reviewer CLIs, and the
# taskdef's mandated path is the wrapper script; an earlier version of these
# fixtures invented a `--post-review` flag and agreed with a hook that had
# invented the same one, which is what TestPatternsMatchRealCommands now
# prevents. The near-misses are the boundary cases the character classes and the
# quote-stripping exist for.
MILESTONE_HITS = [
    ("gate-push", "gate-push"),
    ("gate-push", "bin/gate-push --no-verify"),
    ("gate-push", 'gate-push; work log "pushed the fix round"'),
    ("gate-push", "gate-push && echo ok"),
    ("gitlab-review --create-mr",
     "gitlab-review group/proj --host git.example.com --create-mr --title x"),
    ("gitlab-review --review-file",
     "gitlab-review group/proj 42 --host git.example.com --review-file /tmp/review.json"),
    ("gitlab-review --review-file",
     "gitlab-review group/proj 42 --review-file=/tmp/review.json"),
    # Wrapped invocation: the identifying flag sits after a line continuation.
    ("gitlab-review --review-file",
     "gitlab-review group/proj 42 \\\n  --host git.example.com \\\n"
     "  --review-file /tmp/review.json"),
    ("gitlab-review --reply-thread",
     "gitlab-review group/proj 42 --reply-thread abc --comment-file r.md"),
    ("github-review --review-file",
     "github-review owner/repo 7 --review-file /tmp/review.json"),
    ("gitlab-review-post-review.sh",
     "bash /Agents/global/hooks/gitlab-review-post-review.sh"),
    ("github-review-post-review.sh",
     "bash /Agents/global/hooks/github-review-post-review.sh"),
    # Every legal simple-command prefix counts. The assignment form matters
    # most: the wrappers take no arguments and read three exported variables.
    ("gitlab-review-post-review.sh",
     "GITLAB_PROJECT=g/p GITLAB_MR_ID=42 GITLAB_REVIEW_FILE=/tmp/r.json "
     "bash /Agents/global/hooks/gitlab-review-post-review.sh"),
    ("gitlab-review-post-review.sh",
     "sudo bash -x /Agents/global/hooks/gitlab-review-post-review.sh"),
    ("gitlab-review-post-review.sh",
     "timeout 300 bash /Agents/global/hooks/gitlab-review-post-review.sh"),
    ("gitlab-review-post-review.sh",
     "if true; then bash /Agents/global/hooks/gitlab-review-post-review.sh; fi"),
    ("gitlab-review-post-review.sh", "/Agents/global/hooks/gitlab-review-post-review.sh"),
    ("gitlab-review-post-review.sh", "./gitlab-review-post-review.sh"),
    ("github-review-post-review.sh",
     "{ bash /Agents/global/hooks/github-review-post-review.sh; }"),
    ("gitlab-review-post-review.sh",
     "if false; then true; else bash /Agents/global/hooks/gitlab-review-post-review.sh; fi"),
    ("gitlab-review-post-review.sh",
     "nohup bash /Agents/global/hooks/gitlab-review-post-review.sh &"),
    ("github-review-post-review.sh",
     "cd /tmp && bash hooks/github-review-post-review.sh"),
    ("work state set --status=complete", "work state set --status=complete"),
    ("work state set --status=complete", "work state set --status complete"),
    ("work state set --status=complete",
     'work state set --status=complete --stop-reason=complete; work commit'),
]

MILESTONE_MISSES = [
    'grep "gate-push" notes.md',
    # Measured false positives before quote-stripping: whitespace inside quotes
    # is a boundary like any other, so a command that merely talks about a
    # milestone matched. Three of those burn the whole nudge cap.
    'git commit -m "wire gate-push into CI"',
    'git commit -m "document gitlab-review --review-file in AGENTS.md"',
    'echo "run gate-push before merging"',
    "echo 'and then gate-push'",
    "echo gate-push-docs",
    "gate-pushed",
    "gate-check",
    "gitlab-review group/proj 42 --comments",
    'grep "gitlab-review --review-file" AGENTS.md',
    "github-review owner/repo 7 --docs",
    "bash /Agents/global/hooks/gitlab-review-docs.sh",
    # Reading the wrapper is not running it.
    "cat /Agents/global/hooks/gitlab-review-post-review.sh",
    "ls -l /Agents/global/hooks/github-review-post-review.sh",
    "git log --oneline -- hooks/gitlab-review-post-review.sh",
    "grep -n glab /Agents/global/hooks/github-review-post-review.sh",
    "wc -l /Agents/global/hooks/gitlab-review-post-review.sh",
    "head -20 hooks/github-review-post-review.sh",
    "diff hooks/gitlab-review-post-review.sh hooks/github-review-post-review.sh",
    # Unquoted braces in the *argument* path: the `{` that anchors
    # `{ bash …; }` must not anchor a `${VAR}` expansion.
    "cat ${REPO}/hooks/gitlab-review-post-review.sh",
    "ls -l ${LMER_GLOBAL_DIR}/hooks/github-review-post-review.sh",
    "git log -- ${d}/hooks/gitlab-review-post-review.sh",
    "grep -n GITLAB ${REPO}/hooks/gitlab-review-post-review.sh",
    "wc -l ${HOME}/hooks/gitlab-review-post-review.sh",
    "cat /Agents/{global,work}/hooks/gitlab-review-post-review.sh",
    "work state set --status=blocked",
    "work state set --phase=develop",
    "echo done",
    "",
]


@pytest.mark.parametrize(
    ("wrapper", "reviewer", "review_env", "signal_text"),
    [
        (
            "gitlab-review-post-review.sh",
            "gitlab-review",
            {
                "GITLAB_PROJECT": "agents/global",
                "GITLAB_MR_ID": "42",
                "GITLAB_REVIEW_FILE": "/tmp/review.json",
            },
            "Posted GitLab review for agents/global!42",
        ),
        (
            "github-review-post-review.sh",
            "github-review",
            {
                "GITHUB_PROJECT": "agents/global",
                "GITHUB_PR_ID": "42",
                "GITHUB_REVIEW_FILE": "/tmp/review.json",
                "GITHUB_HOST": "github.example.com",
            },
            "Posted GitHub review for agents/global#42",
        ),
    ],
)
class TestPostReviewWrappers:
    @staticmethod
    def _fake_commands(tmp_path, reviewer):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        review = bin_dir / reviewer
        review.write_text(
            "#!/bin/sh\n"
            "printf 'review %s\\n' \"$*\" >> \"$CALL_LOG\"\n"
            "exit \"${REVIEW_EXIT:-0}\"\n"
        )
        review.chmod(0o755)
        signal = bin_dir / "lmer-signal"
        signal.write_text(
            "#!/bin/sh\n"
            "printf 'signal %s\\n' \"$*\" >> \"$CALL_LOG\"\n"
            "exit \"${SIGNAL_EXIT:-0}\"\n"
        )
        signal.chmod(0o755)
        return bin_dir

    def _run(self, tmp_path, wrapper, reviewer, review_env, review_exit="0",
             signal_exit="0"):
        call_log = tmp_path / "calls"
        bin_dir = self._fake_commands(tmp_path, reviewer)
        env = os.environ.copy()
        env.update(review_env)
        env.update(
            {
                "CALL_LOG": str(call_log),
                "PATH": f"{bin_dir}:{env['PATH']}",
                "REVIEW_EXIT": review_exit,
                "SIGNAL_EXIT": signal_exit,
            }
        )
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "hooks" / wrapper)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        calls = call_log.read_text().splitlines() if call_log.exists() else []
        return result, calls

    def test_success_signals_after_posting(
        self, tmp_path, wrapper, reviewer, review_env, signal_text
    ):
        result, calls = self._run(tmp_path, wrapper, reviewer, review_env)

        assert result.returncode == 0, result.stderr
        assert calls[0].startswith("review ")
        assert calls[1:] == [f"signal {signal_text}"]

    def test_failure_preserves_status_and_does_not_signal(
        self, tmp_path, wrapper, reviewer, review_env, signal_text
    ):
        result, calls = self._run(
            tmp_path, wrapper, reviewer, review_env, review_exit="23"
        )

        assert result.returncode == 23
        assert len(calls) == 1
        assert calls[0].startswith("review ")

    @pytest.mark.parametrize("signal_exit", ["3", "127"])
    def test_signal_failure_does_not_turn_a_posted_review_into_a_failure(
        self, tmp_path, wrapper, reviewer, review_env, signal_text, signal_exit
    ):
        result, calls = self._run(
            tmp_path, wrapper, reviewer, review_env, signal_exit=signal_exit
        )

        assert result.returncode == 0
        assert calls[0].startswith("review ")
        assert calls[1:] == [f"signal {signal_text}"]
        assert "review posted but milestone was not signalled" in result.stderr


# ---- transcript event builders ------------------------------------------------

def _assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def _assistant_bash(command, tool_id=None):
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    if tool_id is not None:
        block["id"] = tool_id
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def _user_text(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text, tool_use_id=None, is_error=False):
    block = {"type": "tool_result", "content": text, "is_error": is_error}
    if tool_use_id is not None:
        block["tool_use_id"] = tool_use_id
    return {"type": "user", "toolUseResult": {"stdout": text},
            "message": {"role": "user", "content": [block]}}


# ---- env_flag -----------------------------------------------------------------

class TestEnvFlag:
    @pytest.mark.parametrize("value", ["1", "yes", "true", "TRUE", " Yes "])
    def test_truthy(self, value):
        assert env_flag(value) is True

    @pytest.mark.parametrize("value", ["0", "no", "false", "FALSE", " No "])
    def test_falsy(self, value):
        assert env_flag(value) is False

    @pytest.mark.parametrize("value", [None, "", "   ", "maybe"])
    def test_unset_blank_or_unknown_defaults_on(self, value):
        assert env_flag(value) is True


# ---- milestone patterns -------------------------------------------------------

class TestMilestonePatterns:
    @pytest.mark.parametrize("label,command", MILESTONE_HITS)
    def test_matches_real_invocations(self, label, command):
        assert unsignalled_milestone([_assistant_bash(command)])[0] == label

    @pytest.mark.parametrize("command", MILESTONE_MISSES)
    def test_ignores_non_milestones(self, command):
        assert unsignalled_milestone([_assistant_bash(command)]) is None

    def test_non_bash_tool_use_never_counts(self):
        block = {"type": "tool_use", "name": "Write",
                 "input": {"file_path": "/tmp/x", "content": "gate-push"}}
        events = [{"type": "assistant",
                   "message": {"role": "assistant", "content": [block]}}]
        assert unsignalled_milestone(events) is None

    def test_prose_mentioning_a_milestone_never_counts(self):
        assert unsignalled_milestone([_assistant_text("next I run gate-push")]) is None

    def test_malformed_events_skipped(self):
        events = [None, "junk", {"type": "assistant"}, _assistant_bash("gate-push")]
        assert unsignalled_milestone(events)[0] == "gate-push"

    def test_a_quoted_mention_beside_a_real_invocation_still_counts(self):
        # Stripping the quoted span must not swallow the real command next to it.
        events = [_assistant_bash('echo "about to gate-push" && gate-push')]
        assert unsignalled_milestone(events)[0] == "gate-push"

    def test_an_unquoted_mention_in_a_heredoc_body_is_a_known_false_positive(self):
        # Measured limit, documented rather than claimed away: quote-stripping
        # cannot see heredoc bodies, so prose there still reads as an
        # invocation. The nudge cap is what bounds the cost.
        command = "cat <<EOF > notes.md\nrun gate-push before merging\nEOF"
        assert unsignalled_milestone([_assistant_bash(command)])[0] == "gate-push"

    def test_the_wrapper_script_does_not_read_as_the_bare_cli(self):
        # `gitlab-review-post-review.sh` must not satisfy the `gitlab-review\s…`
        # rows (no whitespace follows the CLI name) — the label proves which row
        # matched.
        command = "bash /Agents/global/hooks/gitlab-review-post-review.sh"
        assert unsignalled_milestone([_assistant_bash(command)])[0] == \
            "gitlab-review-post-review.sh"


# ---- pattern list vs. reality (drift guard) -----------------------------------

def _option_strings(parser):
    """Every ``--flag`` an argparse parser accepts, subparsers included."""
    import argparse

    options = set()
    for action in parser._actions:
        options.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                options |= _option_strings(sub)
    return options


def _flag_owners():
    """Label prefix -> the CLI parser that must accept the label's flag.

    Imported here rather than at module scope: the hook itself imports no
    project code, and this test is the seam that checks the hook's *spellings*
    against the code it describes.
    """
    from github_reviewer import cli as github_cli
    from gitlab_reviewer import cli as gitlab_cli
    from work_repo import cli as work_cli

    return {
        "gitlab-review": gitlab_cli.create_parser,
        "github-review": github_cli.create_parser,
        "work state set": work_cli.create_parser,
    }


class TestPatternsMatchRealCommands:
    """The pattern list names real commands and real flags.

    The first version of it matched `--post-review`, a flag neither reviewer CLI
    has ever had (both post with `--review-file`), and the fixtures had invented
    the same flag — so the suite was green while the detector could not fire on
    the mandated path. This class binds every row to the code it claims to
    describe, so a flag renamed upstream fails here instead of silently.
    """

    def test_every_row_is_classified(self):
        """A new row must be a flag row, a script row, or a bin row — so adding
        one to the hook without a reality check fails here."""
        for label, _pattern in _MILESTONE_PATTERNS:
            assert (
                label.endswith(".sh")
                or " --" in label
                or (REPO_ROOT / "bin" / label).exists()
            ), f"unclassified milestone row: {label}"

    @pytest.mark.parametrize("label", [
        label for label, _ in _MILESTONE_PATTERNS if " --" in label
    ])
    def test_flag_rows_name_a_real_flag(self, label):
        prefix, flag = label.split(" --", 1)
        flag = "--" + flag.split("=", 1)[0]
        owners = _flag_owners()
        assert prefix in owners, f"no parser mapped for {prefix}"
        assert flag in _option_strings(owners[prefix]()), (
            f"{label}: {flag} is not an option of the {prefix} CLI")

    @pytest.mark.parametrize("label", [
        label for label, _ in _MILESTONE_PATTERNS if label.endswith(".sh")
    ])
    def test_script_rows_name_a_real_hook_script(self, label):
        assert (REPO_ROOT / "hooks" / label).exists(), \
            f"{label} is not in hooks/"

    def test_wrapper_scripts_take_no_positional_arguments(self):
        """The wrappers accept nothing on the command line, which is why the
        fixtures spell them bare or assignment-prefixed."""
        for label, _ in _MILESTONE_PATTERNS:
            if not label.endswith("-post-review.sh"):
                continue
            body = (REPO_ROOT / "hooks" / label).read_text()
            assert not re.search(r"\$\{?[1-9@*]", body), (
                f"{label} now reads positional arguments — the fixtures spell it "
                "as a command with none"
            )

    def test_bin_rows_name_a_real_bin_script(self):
        bare = [label for label, _ in _MILESTONE_PATTERNS
                if " --" not in label and not label.endswith(".sh")]
        assert bare, "expected at least one bare-command row (gate-push)"
        for label in bare:
            assert (REPO_ROOT / "bin" / label).exists()

    def test_the_completion_status_value_is_a_real_choice(self):
        # The row matches `--status=complete` specifically, so the *value* has to
        # exist too, not just the flag.
        from work_repo import cli as work_cli

        parser = work_cli.create_parser()
        choices = [
            action.choices
            for action in _status_actions(parser)
            if action.choices
        ]
        assert any("complete" in choice for choice in choices), \
            "`complete` is not a --status choice on the work CLI"


def _status_actions(parser):
    """Every ``--status`` action in a parser tree."""
    import argparse

    found = []
    for action in parser._actions:
        if "--status" in action.option_strings:
            found.append(action)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found.extend(_status_actions(sub))
    return found


# ---- is_error correlation -----------------------------------------------------

class TestIsErrorCorrelation:
    def test_failed_milestone_does_not_trip(self):
        events = [
            _assistant_bash("gate-push", tool_id="t1"),
            _tool_result("Exit code 1: tests failed", tool_use_id="t1", is_error=True),
        ]
        assert unsignalled_milestone(events) is None

    def test_successful_milestone_trips(self):
        events = [
            _assistant_bash("gate-push", tool_id="t1"),
            _tool_result("pushed", tool_use_id="t1", is_error=False),
        ]
        assert unsignalled_milestone(events)[0] == "gate-push"

    def test_uncorrelatable_milestone_counts_as_succeeded(self):
        # A real milestone must not be dropped silently; the cost of being
        # wrong here is one reminder.
        events = [_assistant_bash("gate-push", tool_id="t1"),
                  _tool_result("pushed", tool_use_id="t-other")]
        assert unsignalled_milestone(events)[0] == "gate-push"

    def test_failed_signal_does_not_suppress(self):
        events = [
            _assistant_bash("gate-push", tool_id="t1"),
            _tool_result("pushed", tool_use_id="t1"),
            _assistant_bash('lmer-signal "pushed"', tool_id="t2"),
            _tool_result("Exit code 3: no channel", tool_use_id="t2", is_error=True),
        ]
        assert unsignalled_milestone(events)[0] == "gate-push"

    def test_uncorrelatable_signal_counts_as_delivered(self):
        # Opposite direction from the milestone: fail toward not nagging a
        # session that did the right thing.
        events = [
            _assistant_bash("gate-push", tool_id="t1"),
            _assistant_bash('lmer-signal "pushed"', tool_id="t2"),
            _tool_result("ok", tool_use_id="t-other"),
        ]
        assert unsignalled_milestone(events) is None

    def test_failed_then_retried_signal_suppresses(self):
        events = [
            _assistant_bash("gate-push", tool_id="t1"),
            _assistant_bash('lmer-signal "pushed"', tool_id="t2"),
            _tool_result("Exit code 3", tool_use_id="t2", is_error=True),
            _assistant_bash('lmer-signal "pushed"', tool_id="t3"),
            _tool_result("ok", tool_use_id="t3"),
        ]
        assert unsignalled_milestone(events) is None


# ---- ordering -----------------------------------------------------------------

class TestOrdering:
    def test_milestone_then_signal_is_silent(self):
        events = [
            _user_text("push the fixes"),
            _assistant_bash("gate-push"),
            _assistant_bash('lmer-signal "fixes pushed"'),
        ]
        assert unsignalled_milestone(events) is None

    def test_signal_then_milestone_still_pending(self):
        # The run signalled its previous milestone and then produced another.
        events = [
            _assistant_bash("gate-push", tool_id="t1"),
            _assistant_bash('lmer-signal "round 1 pushed"', tool_id="t2"),
            _assistant_bash("gitlab-review group/proj 42 --review-file r.json",
                            tool_id="t3"),
        ]
        milestone = unsignalled_milestone(events)
        assert milestone == ("gitlab-review --review-file", "t3")

    def test_newest_milestone_wins_the_key(self):
        events = [_assistant_bash("gate-push", tool_id="t1"),
                  _assistant_bash("gate-push", tool_id="t2")]
        assert unsignalled_milestone(events)[1] == "t2"

    def test_missing_tool_id_falls_back_to_the_pattern_key(self):
        assert unsignalled_milestone([_assistant_bash("gate-push")])[1] == \
            "pattern:gate-push"


class TestSignalRecognition:
    """The mirror of the milestone-boundary cases: a *mention* of lmer-signal
    must not clear a genuinely pending milestone."""

    @pytest.mark.parametrize("command", [
        'echo "then run lmer-signal when done"',
        "echo 'remember lmer-signal'",
        'grep "lmer-signal" prompts/orchestrator-ask.md',
        'work log "will lmer-signal after the gate"',
    ])
    def test_a_quoted_mention_does_not_suppress(self, command):
        events = [_assistant_bash("gate-push", tool_id="t1"),
                  _assistant_bash(command, tool_id="t2")]
        assert unsignalled_milestone(events) == ("gate-push", "t1")

    @pytest.mark.parametrize("command", [
        'lmer-signal "round 1 posted"',
        "lmer-signal --stdin",
        "/usr/local/bin/lmer-signal 'done'",
        'echo "posted" && lmer-signal "round 1 posted"',
    ])
    def test_real_invocations_suppress(self, command):
        events = [_assistant_bash("gate-push", tool_id="t1"),
                  _assistant_bash(command, tool_id="t2")]
        assert unsignalled_milestone(events) is None


class TestTranscriptEndsSignalled:
    """What the facts side consults: the completed-run fact persists for the
    whole session, so it must not outlive the signal that already reported it."""

    def test_milestone_then_signal_ends_signalled(self):
        events = [_assistant_bash("work state set --status=complete", tool_id="t1"),
                  _assistant_bash('lmer-signal "run complete"', tool_id="t2")]
        assert transcript_ends_signalled(events) is True

    def test_signal_alone_ends_signalled(self):
        assert transcript_ends_signalled(
            [_assistant_bash('lmer-signal "done"')]) is True

    def test_no_signal_at_all(self):
        assert transcript_ends_signalled([_assistant_bash("gate-push")]) is False

    def test_a_milestone_after_the_signal_is_not_signalled(self):
        events = [_assistant_bash('lmer-signal "round 1"', tool_id="t1"),
                  _assistant_bash("gate-push", tool_id="t2")]
        assert transcript_ends_signalled(events) is False

    def test_a_failed_signal_does_not_count(self):
        events = [_assistant_bash('lmer-signal "done"', tool_id="t1"),
                  _tool_result("Exit code 3", tool_use_id="t1", is_error=True)]
        assert transcript_ends_signalled(events) is False

    def test_a_quoted_mention_does_not_count(self):
        assert transcript_ends_signalled(
            [_assistant_bash('echo "remember to lmer-signal"')]) is False


class TestAdvancedBaseline:
    def test_advances_to_a_newer_signal(self):
        assert advanced_baseline(3, 5) == 5

    def test_seeds_from_none(self):
        assert advanced_baseline(None, 2) == 2

    @pytest.mark.parametrize("baseline,newest", [(5, None), (5, 4), (5, 5), (None, None)])
    def test_never_regresses(self, baseline, newest):
        # A baseline that moved backwards would make an accounted-for signal
        # look new again and re-suppress a later milestone.
        assert advanced_baseline(baseline, newest) == baseline


# ---- facts side ---------------------------------------------------------------

class TestFactMilestone:
    def test_completed_run_flag(self):
        assert fact_milestone({"kind": "run", "completed_run": True})[1] == \
            FACT_MILESTONE_KEY

    def test_status_complete(self):
        assert fact_milestone({"kind": "run", "status": "complete"})[1] == \
            FACT_MILESTONE_KEY

    @pytest.mark.parametrize("decision", [
        {"kind": "run", "completed_run": False, "status": "active"},
        {"kind": "run", "status": "blocked"},
        {},
        None,
        "not a dict",
    ])
    def test_anything_else_is_no_evidence(self, decision):
        assert fact_milestone(decision) is None


class TestParseResumeJson:
    def test_single_line(self):
        assert parse_resume_json('{"kind": "run"}') == {"kind": "run"}

    def test_last_parsable_line_wins(self):
        stdout = 'warning\n{"kind": "run", "status": "active"}\nnot json\n'
        assert parse_resume_json(stdout) == {"kind": "run", "status": "active"}

    @pytest.mark.parametrize("stdout", ["", "  \n ", "No run context.", "[1,2]", "42"])
    def test_unrecoverable_is_none(self, stdout):
        assert parse_resume_json(stdout) is None


# ---- channel-dir facts --------------------------------------------------------

class TestNewestSignalId:
    def test_highest_id_wins(self):
        names = ["000001" + SIGNAL_SUFFIX, "000012" + SIGNAL_SUFFIX,
                 "000007" + SIGNAL_SUFFIX]
        assert newest_signal_id(names) == 12

    def test_no_signals_is_none(self):
        assert newest_signal_id(["000001" + QUESTION_SUFFIX, "notes.txt"]) is None

    def test_non_numeric_stem_ignored(self):
        assert newest_signal_id(["draft" + SIGNAL_SUFFIX]) is None

    def test_empty_dir(self):
        assert newest_signal_id([]) is None


class TestHasOpenQuestion:
    def test_bare_question_is_open(self):
        assert has_open_question(["000001" + QUESTION_SUFFIX]) is True

    def test_answered_question_is_not_open(self):
        assert has_open_question(
            ["000001" + QUESTION_SUFFIX, "000001" + ANSWER_SUFFIX]) is False

    def test_closed_question_is_not_open(self):
        assert has_open_question(
            ["000001" + QUESTION_SUFFIX, "000001" + CLOSED_SUFFIX]) is False

    def test_one_open_among_settled_ones_counts(self):
        names = ["000001" + QUESTION_SUFFIX, "000001" + ANSWER_SUFFIX,
                 "000002" + QUESTION_SUFFIX]
        assert has_open_question(names) is True

    def test_sidecar_of_another_id_does_not_settle_it(self):
        names = ["000002" + QUESTION_SUFFIX, "000001" + ANSWER_SUFFIX]
        assert has_open_question(names) is True

    def test_no_questions(self):
        assert has_open_question(["000001" + SIGNAL_SUFFIX]) is False


class TestNewestOpenQuestionId:
    """The id, not just a bool: the question suppressor is bounded against a
    recorded baseline exactly like the signal one."""

    def test_highest_open_id_wins(self):
        names = ["000001" + QUESTION_SUFFIX, "000004" + QUESTION_SUFFIX]
        assert newest_open_question_id(names) == 4

    def test_settled_questions_are_skipped(self):
        names = ["000001" + QUESTION_SUFFIX, "000004" + QUESTION_SUFFIX,
                 "000004" + ANSWER_SUFFIX]
        assert newest_open_question_id(names) == 1

    def test_all_settled_is_none(self):
        names = ["000001" + QUESTION_SUFFIX, "000001" + CLOSED_SUFFIX]
        assert newest_open_question_id(names) is None

    def test_no_questions_is_none(self):
        assert newest_open_question_id(["000001" + SIGNAL_SUFFIX]) is None


class TestTranscriptHasSignal:
    def test_true_for_any_successful_signal(self):
        events = [_assistant_bash('lmer-signal "x"', tool_id="t1"),
                  _assistant_bash("gate-push", tool_id="t2")]
        assert transcript_has_signal(events) is True   # even with work after it

    def test_false_without_one(self):
        assert transcript_has_signal([_assistant_bash("gate-push")]) is False

    def test_false_when_the_signal_failed(self):
        events = [_assistant_bash('lmer-signal "x"', tool_id="t1"),
                  _tool_result("Exit code 3", tool_use_id="t1", is_error=True)]
        assert transcript_has_signal(events) is False


class TestSignalIsNew:
    def test_newer_than_baseline(self):
        assert signal_is_new(5, 3) is True

    def test_equal_to_baseline_is_not_new(self):
        assert signal_is_new(3, 3) is False

    def test_older_than_baseline_is_not_new(self):
        assert signal_is_new(2, 3) is False

    def test_no_baseline_means_any_signal_counts(self):
        # First-stop conservatism: fail toward not nagging a session that has
        # already reported itself.
        assert signal_is_new(1, None) is True

    def test_no_signal_never_suppresses(self):
        assert signal_is_new(None, None) is False
        assert signal_is_new(None, 3) is False


# ---- evaluate -----------------------------------------------------------------

class TestEvaluate:
    @staticmethod
    def _evaluate(**kwargs):
        inputs = dict(
            transcript_milestone=("gate-push", "t1"),
            fact_milestone=None,
            question_seen=False,
            signal_seen=False,
        )
        inputs.update(kwargs)
        return evaluate(**inputs)

    def test_unsignalled_milestone_blocks(self):
        verdict = self._evaluate()
        assert "lmer-signal" in verdict["reason"]
        assert "gate-push" in verdict["reason"]
        assert verdict["key"] == "t1"

    def test_no_evidence_is_silent(self):
        assert self._evaluate(transcript_milestone=None)["reason"] is None

    def test_open_question_suppresses(self):
        assert self._evaluate(question_seen=True)["reason"] is None

    def test_signal_fact_suppresses(self):
        assert self._evaluate(signal_seen=True)["reason"] is None

    def test_a_suppressing_signal_file_accounts_for_the_pending_milestone(self):
        assert self._evaluate(signal_seen=True)["accounted_key"] == "t1"

    def test_a_suppressing_question_accounts_for_nothing(self):
        # The question suppressor expires with its own baseline and says nothing
        # about whether the milestone was reported.
        assert self._evaluate(question_seen=True)["accounted_key"] is None

    def test_the_channel_signal_yields_to_the_transcripts_ordering(self):
        # Ordered evidence wins: with a signal in the transcript, the walk has
        # already placed it relative to the milestone, and the channel file is
        # the same event without a position.
        verdict = self._evaluate(signal_seen=True, transcript_has_signal=True)
        assert verdict["reason"] is not None
        assert verdict["accounted_key"] is None

    def test_a_question_still_suppresses_when_the_transcript_has_a_signal(self):
        assert self._evaluate(question_seen=True, transcript_has_signal=True)[
            "reason"] is None

    def test_the_facts_reason_does_not_claim_this_turn(self):
        verdict = self._evaluate(
            transcript_milestone=None,
            fact_milestone=(FACT_MILESTONE_LABEL, FACT_MILESTONE_KEY))
        assert "this turn" not in verdict["reason"]
        assert "no signal was sent this session" in verdict["reason"]

    def test_the_transcript_reason_does_claim_this_turn(self):
        assert "this turn" in self._evaluate()["reason"]

    def test_fact_milestone_blocks_on_its_own(self):
        verdict = self._evaluate(
            transcript_milestone=None, fact_milestone=("run complete", FACT_MILESTONE_KEY))
        assert verdict["key"] == FACT_MILESTONE_KEY
        assert "run complete" in verdict["reason"]

    def test_transcript_milestone_preferred_over_the_fact(self):
        verdict = self._evaluate(fact_milestone=("run complete", FACT_MILESTONE_KEY))
        assert verdict["key"] == "t1"

    def test_already_nudged_key_is_silent(self):
        assert self._evaluate(nudged_keys=["t1"])["reason"] is None

    def test_other_keys_do_not_block_this_one(self):
        assert self._evaluate(nudged_keys=["t0"])["reason"] is not None

    @pytest.mark.parametrize("spent", range(NUDGE_CAP))
    def test_fires_below_cap(self, spent):
        assert self._evaluate(nudges=spent)["reason"] is not None

    @pytest.mark.parametrize("spent", [NUDGE_CAP, NUDGE_CAP + 1])
    def test_capped(self, spent):
        assert self._evaluate(nudges=spent)["reason"] is None

    def test_cap_override(self):
        assert self._evaluate(nudges=1, cap=1)["reason"] is None


class TestBuildReason:
    def test_names_the_command_and_the_opt_out(self):
        reason = build_reason("gate-push")
        assert "gate-push" in reason
        assert 'lmer-signal "<what happened>"' in reason
        assert "just stop again" in reason


# ---- iter_messages ------------------------------------------------------------

class TestIterMessages:
    def test_parses_and_skips_bad_lines(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps(_assistant_bash("gate-push")) + "\nnot json\n\n"
            + json.dumps(_user_text("q")) + "\n"
        )
        events = iter_messages(str(path))
        assert [e["type"] for e in events] == ["assistant", "user"]


# ---- main() via subprocess ----------------------------------------------------

# Env vars the harness controls per test; anything inherited is scrubbed first
# so a dev container's own orchestration context cannot leak into assertions.
GUARD_ENV_VARS = (
    "LMER_SIGNAL_GUARD",
    "LMER_ASK_DIR",
    "LMER_SESSION_ID",
)


@pytest.fixture
def session():
    """Unique per-test session id; removes the /tmp marker afterwards.

    The hook keys its marker on LMER_SESSION_ID with a hardcoded /tmp
    template, so isolation between tests (and parallel workers) comes from the
    uuid, and cleanup keeps /tmp from accumulating."""
    sid = f"pytest-{uuid.uuid4().hex}"
    yield sid
    try:
        os.unlink(MARKER_TEMPLATE.format(session=sid))
    except OSError:
        pass


@pytest.fixture
def empty_bin(tmp_path):
    """A PATH holding no `work` binary.

    The default for these tests: with a real `work` on PATH the facts side
    would consult this container's own run, so the transcript assertions would
    depend on the environment running them."""
    path = tmp_path / "empty-bin"
    path.mkdir()
    return path


def _channel_dir(tmp_path, names=()):
    """A channel dir holding the given entry files (contents are never read)."""
    ask_dir = tmp_path / "ask"
    ask_dir.mkdir(exist_ok=True)
    for name in names:
        (ask_dir / name).write_text("{}\n")
    return ask_dir


def _write_transcript(tmp_path, events):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return str(path)


def _make_work_cli(tmp_path, decision=None, exit_code=0, raw=None):
    """Fake `work` CLI on PATH that prints a canned resume payload.

    Shell builtins only (`echo`, not a here-doc through `cat`): PATH holds this
    directory and nothing else, so the container's real `work` cannot answer
    for the fake one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    body = raw if raw is not None else json.dumps(decision)
    script = bin_dir / "work"
    script.write_text(f"#!/bin/sh\necho '{body}'\nexit {exit_code}\n")
    script.chmod(0o755)
    return bin_dir


def _guard_env(bin_dir, session, ask_dir):
    return {
        "PATH": str(bin_dir),
        "LMER_ASK_DIR": str(ask_dir),
        "LMER_SESSION_ID": session,
    }


def _run_hook(payload, env_extra, raw_stdin=None):
    env = os.environ.copy()
    for var in GUARD_ENV_VARS:
        env.pop(var, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=raw_stdin if raw_stdin is not None else json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


MILESTONE_TURN = [
    _user_text("post the review"),
    _assistant_bash("gitlab-review group/proj 42 --host git.example.com "
                    "--review-file /tmp/review.json", tool_id="t1"),
    _tool_result("posted", tool_use_id="t1"),
    _assistant_text("round 1 is posted"),
]


class TestMainTranscriptTrigger:
    def test_blocks_on_unsignalled_milestone(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "lmer-signal" in out["reason"]
        assert "gitlab-review --review-file" in out["reason"]

    def test_silent_when_the_signal_came_after(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(
            tmp_path, MILESTONE_TURN + [_assistant_bash('lmer-signal "round 1 posted"')])
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_silent_on_a_mid_task_stop(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, [
            _user_text("look into the failure"),
            _assistant_bash("python -m pytest tests/test_x.py -q"),
            _assistant_text("the failure is in the fixture"),
        ])
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_silent_when_the_milestone_command_failed(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, [
            _assistant_bash("gate-push", tool_id="t1"),
            _tool_result("Exit code 1: suite failed", tool_use_id="t1", is_error=True),
        ])
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestMainChannelSuppressors:
    def test_open_question_suppresses(self, tmp_path, session, empty_bin):
        # Ask-equivalence: `lmer-ask ask` exits 2 while posted-and-waiting, so
        # the file check — never is_error — is what recognizes this turn.
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        ask_dir = _channel_dir(tmp_path, ["000001" + QUESTION_SUFFIX])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, ask_dir))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_answered_question_does_not_suppress(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        ask_dir = _channel_dir(
            tmp_path, ["000001" + QUESTION_SUFFIX, "000001" + ANSWER_SUFFIX])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, ask_dir))
        assert json.loads(r.stdout)["decision"] == "block"

    def test_closed_question_does_not_suppress(self, tmp_path, session, empty_bin):
        # A withdrawn question notifies nobody, so it is not signal-equivalent.
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        ask_dir = _channel_dir(
            tmp_path, ["000001" + QUESTION_SUFFIX, "000001" + CLOSED_SUFFIX])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, ask_dir))
        assert json.loads(r.stdout)["decision"] == "block"

    def test_signal_file_suppresses_without_a_baseline(self, tmp_path, session, empty_bin):
        # Covers a signal sent through a wrapper the transcript regex misses.
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        ask_dir = _channel_dir(tmp_path, ["000003" + SIGNAL_SUFFIX])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, ask_dir))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_a_signal_file_accounts_for_the_pending_milestone(
        self, tmp_path, session, empty_bin
    ):
        """A wrapper signal that suppresses a pending milestone must also mark
        it accounted-for. The suppression expires with the baseline while the
        milestone stays in the transcript, so without this the very next stop
        would nudge for work the signal already covered."""
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        ask_dir = _channel_dir(tmp_path, ["000003" + SIGNAL_SUFFIX])
        env = _guard_env(empty_bin, session, ask_dir)

        first = _run_hook({"transcript_path": transcript}, env)
        assert first.stdout.strip() == ""
        marker = json.loads(Path(MARKER_TEMPLATE.format(session=session)).read_text())
        assert marker["signal_baseline"] == 3
        assert marker["keys"] == ["t1"]      # the suppressed milestone
        assert marker["nudges"] == 0         # a suppressed stop spends no nudge

        # Same turn, no new work: still silent.
        second = _run_hook({"transcript_path": transcript}, env)
        assert second.returncode == 0
        assert second.stdout.strip() == ""

    def test_a_wrapper_signal_suppresses_one_stop_and_not_the_session(
        self, tmp_path, session, empty_bin
    ):
        """The channel-dir suppressor must be bounded to the stop that sees the
        signal. A baseline left behind keeps its signal "newer" forever, which
        would silence every later milestone in the session — the silent miss
        this hook exists to catch."""
        ask_dir = _channel_dir(tmp_path, ["000001" + SIGNAL_SUFFIX])
        env = _guard_env(empty_bin, session, ask_dir)
        marker_path = Path(MARKER_TEMPLATE.format(session=session))

        # Stop 1: no milestone at all — silent, and it establishes the baseline.
        idle = _write_transcript(tmp_path, [_assistant_text("still working")])
        assert _run_hook({"transcript_path": idle}, env).stdout.strip() == ""
        assert json.loads(marker_path.read_text())["signal_baseline"] == 1

        # Stop 2: a signal appears through a wrapper the transcript cannot see,
        # alongside an unsignalled milestone — suppressed, baseline advances.
        (ask_dir / ("000002" + SIGNAL_SUFFIX)).write_text("{}\n")
        first_milestone = _write_transcript(
            tmp_path, [_assistant_bash("gate-push", tool_id="t1")])
        assert _run_hook({"transcript_path": first_milestone}, env).stdout.strip() == ""
        marker = json.loads(marker_path.read_text())
        assert marker["signal_baseline"] == 2
        assert marker["nudges"] == 0  # a suppressed stop spends no nudge

        # Stop 3: a further unsignalled milestone, no new signal file — the
        # suppression is spent, so this one is nudged.
        second_milestone = _write_transcript(
            tmp_path, [_assistant_bash("gate-push", tool_id="t1"),
                       _assistant_bash("gate-push", tool_id="t2")])
        third = _run_hook({"transcript_path": second_milestone}, env)
        assert json.loads(third.stdout)["decision"] == "block"

    def test_a_newer_signal_suppresses_the_next_milestone_after_a_nudge(
        self, tmp_path, session, empty_bin
    ):
        ask_dir = _channel_dir(tmp_path)          # no signals yet
        env = _guard_env(empty_bin, session, ask_dir)

        # Stop 1: unsignalled milestone, empty channel dir → nudged.
        first_turn = _write_transcript(tmp_path, [_assistant_bash("gate-push", "t1")])
        assert json.loads(_run_hook({"transcript_path": first_turn}, env).stdout)[
            "decision"] == "block"

        # Stop 2: a signal lands through a wrapper, and a further milestone
        # arrives with it → the signal accounts for it, so this stop is silent.
        (ask_dir / ("000001" + SIGNAL_SUFFIX)).write_text("{}\n")
        second_turn = _write_transcript(
            tmp_path, [_assistant_bash("gate-push", "t1"), _assistant_bash("gate-push", "t2")])
        second = _run_hook({"transcript_path": second_turn}, env)
        assert second.stdout.strip() == ""

        # Stop 3: yet another milestone, no new signal → the suppression is
        # spent and this one is nudged.
        third_turn = _write_transcript(
            tmp_path, [_assistant_bash("gate-push", "t1"), _assistant_bash("gate-push", "t2"),
                       _assistant_bash("gate-push", "t3")])
        assert json.loads(_run_hook({"transcript_path": third_turn}, env).stdout)[
            "decision"] == "block"


class TestReviewReplays:
    """End-to-end replays of the sequences an iteration-1 review of !222 found.
    Each one is a whole nudge cycle, which is where the interactions between the
    transcript walk, the channel dir and the marker actually show up."""

    def test_the_hooks_own_success_path_does_not_swallow_the_next_milestone(
        self, tmp_path, session, empty_bin
    ):
        """nudge → the agent signals → new milestone → must nudge again.

        The stop right after a successful nudge carries `stop_hook_active`, and
        it is the only stop where the signal that nudge produced is visible. When
        that stop skipped its bookkeeping, the signal read as "new" one stop
        later, suppressed a genuinely new milestone, and the suppression path
        then recorded that milestone as accounted-for — permanently."""
        ask_dir = _channel_dir(tmp_path)
        env = _guard_env(empty_bin, session, ask_dir)
        marker_path = Path(MARKER_TEMPLATE.format(session=session))

        # Stop 1 — pushed, nothing signalled.
        turn_one = [_assistant_bash("gate-push", tool_id="t1"),
                    _tool_result("pushed", tool_use_id="t1")]
        first = _run_hook({"transcript_path": _write_transcript(tmp_path, turn_one)}, env)
        assert json.loads(first.stdout)["decision"] == "block"

        # Stop 2 — the agent obeys: signals, then stops again. Claude Code sets
        # stop_hook_active on this stop, and the signal file now exists.
        (ask_dir / ("000001" + SIGNAL_SUFFIX)).write_text("{}\n")
        turn_two = turn_one + [
            _assistant_bash('lmer-signal "pushed the fix round"', tool_id="t2"),
            _tool_result("signal 000001 posted", tool_use_id="t2"),
        ]
        transcript_two = _write_transcript(tmp_path, turn_two)
        second = _run_hook(
            {"transcript_path": transcript_two, "stop_hook_active": True}, env)
        assert second.returncode == 0
        assert second.stdout.strip() == ""
        assert json.loads(marker_path.read_text())["signal_baseline"] == 1

        # Stop 3 — same state, a normal stop: nothing new to report.
        third = _run_hook({"transcript_path": transcript_two}, env)
        assert third.stdout.strip() == ""

        # Stop 4 — a genuinely new milestone, still unsignalled: must nudge.
        turn_four = turn_two + [
            _assistant_bash("gitlab-review group/proj 42 --review-file r.json",
                            tool_id="t3"),
            _tool_result("posted", tool_use_id="t3"),
        ]
        fourth = _run_hook({"transcript_path": _write_transcript(tmp_path, turn_four)}, env)
        out = json.loads(fourth.stdout)
        assert out["decision"] == "block"
        assert "gitlab-review --review-file" in out["reason"]

    def test_a_channel_signal_does_not_overrule_the_transcripts_ordering(
        self, tmp_path, session, empty_bin
    ):
        """signal → milestone inside one interval, with the signal's file in the
        channel dir. The file is that same signal seen a second time, so letting
        it suppress would put unordered evidence above the ordered walk and
        silence the milestone that came after."""
        transcript = _write_transcript(tmp_path, [
            _assistant_bash('lmer-signal "round 1 posted"', tool_id="t1"),
            _tool_result("signal 000001 posted", tool_use_id="t1"),
            _assistant_bash("gate-push", tool_id="t2"),
            _tool_result("pushed", tool_use_id="t2"),
        ])
        ask_dir = _channel_dir(tmp_path, ["000001" + SIGNAL_SUFFIX])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, ask_dir))
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "gate-push" in out["reason"]

    def test_an_open_question_suppresses_once_and_not_the_whole_session(
        self, tmp_path, session, empty_bin
    ):
        """A question can stay open for a long time — the prompt tells the agent
        to keep working while it waits — so "any open question" silenced every
        milestone for the rest of the session."""
        ask_dir = _channel_dir(tmp_path, ["000001" + QUESTION_SUFFIX])
        env = _guard_env(empty_bin, session, ask_dir)
        marker_path = Path(MARKER_TEMPLATE.format(session=session))

        # Stop 1 — asked a question and pushed: the ask channel already told the
        # orchestrator where this run is.
        turn_one = [_assistant_bash("gate-push", tool_id="t1"),
                    _tool_result("pushed", tool_use_id="t1")]
        first = _run_hook({"transcript_path": _write_transcript(tmp_path, turn_one)}, env)
        assert first.stdout.strip() == ""
        assert json.loads(marker_path.read_text())["question_baseline"] == 1

        # Stop 2 — the question is still open, but there is new reportable work.
        turn_two = turn_one + [
            _assistant_bash("gitlab-review group/proj --create-mr --title x",
                            tool_id="t2"),
            _tool_result("MR created", tool_use_id="t2"),
        ]
        second = _run_hook({"transcript_path": _write_transcript(tmp_path, turn_two)}, env)
        out = json.loads(second.stdout)
        assert out["decision"] == "block"
        assert "gitlab-review --create-mr" in out["reason"]

    def test_a_newly_posted_question_suppresses_again(self, tmp_path, session, empty_bin):
        ask_dir = _channel_dir(tmp_path, ["000001" + QUESTION_SUFFIX])
        env = _guard_env(empty_bin, session, ask_dir)
        turn_one = [_assistant_bash("gate-push", tool_id="t1")]
        assert _run_hook(
            {"transcript_path": _write_transcript(tmp_path, turn_one)}, env
        ).stdout.strip() == ""

        # A second question, and new work beside it: the fresh ask notifies the
        # orchestrator again, so this stop is suppressed too.
        (ask_dir / ("000002" + QUESTION_SUFFIX)).write_text("{}\n")
        turn_two = turn_one + [
            _assistant_bash("gitlab-review group/proj --create-mr --title x", tool_id="t2")]
        assert _run_hook(
            {"transcript_path": _write_transcript(tmp_path, turn_two)}, env
        ).stdout.strip() == ""

    def test_an_answered_question_stops_suppressing_immediately(
        self, tmp_path, session, empty_bin
    ):
        ask_dir = _channel_dir(tmp_path, ["000001" + QUESTION_SUFFIX])
        env = _guard_env(empty_bin, session, ask_dir)
        transcript = _write_transcript(tmp_path, [_assistant_bash("gate-push", "t1")])
        assert _run_hook({"transcript_path": transcript}, env).stdout.strip() == ""

        (ask_dir / ("000001" + ANSWER_SUFFIX)).write_text("{}\n")
        answered = _run_hook({"transcript_path": transcript}, env)
        assert json.loads(answered.stdout)["decision"] == "block"


class TestMainGates:
    def test_kill_switch_disables(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        env["LMER_SIGNAL_GUARD"] = "0"
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
    def test_fan_out_children_are_skipped(self, tmp_path, session, empty_bin, value):
        """A `claude -p` child's only output is its last turn, so a Stop block
        replaces the result its parent is waiting for. spawn-harness marks every
        child with LMER_NONINTERACTIVE, and a child reports to its parent rather
        than to the orchestrator anyway."""
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        env["LMER_NONINTERACTIVE"] = value
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""
        assert not Path(MARKER_TEMPLATE.format(session=session)).exists()

    @pytest.mark.parametrize("value", ["0", "false", "no", ""])
    def test_an_attended_session_is_not_skipped(self, tmp_path, session, empty_bin, value):
        # Unset and falsy both mean "a human/orchestrator is on the other end",
        # so the default must not be "skip".
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        env["LMER_NONINTERACTIVE"] = value
        r = _run_hook({"transcript_path": transcript}, env)
        assert json.loads(r.stdout)["decision"] == "block"

    def test_explicitly_enabled_still_blocks(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        env["LMER_SIGNAL_GUARD"] = "1"
        r = _run_hook({"transcript_path": transcript}, env)
        assert json.loads(r.stdout)["decision"] == "block"

    def test_silent_when_stop_hook_active(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        r = _run_hook({"transcript_path": transcript, "stop_hook_active": True}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_silent_without_an_ask_dir(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        del env["LMER_ASK_DIR"]
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    @pytest.mark.parametrize("value", ["", "   "])
    def test_silent_on_a_blank_ask_dir(self, tmp_path, session, empty_bin, value):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        env["LMER_ASK_DIR"] = value
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_silent_when_the_ask_dir_is_missing(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, tmp_path / "no-such-channel")
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestMainFactsTrigger:
    def test_completed_run_blocks_when_unsignalled(self, tmp_path, session):
        bin_dir = _make_work_cli(
            tmp_path, {"kind": "run", "slug": "s", "completed_run": True,
                       "status": "complete"})
        # No transcript milestone at all: the facts side is what fires.
        transcript = _write_transcript(tmp_path, [_assistant_text("wrapping up")])
        env = _guard_env(bin_dir, session, _channel_dir(tmp_path))
        r = _run_hook({"transcript_path": transcript}, env)
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "lmer-signal" in out["reason"]

    def test_active_run_is_silent(self, tmp_path, session):
        bin_dir = _make_work_cli(
            tmp_path, {"kind": "run", "completed_run": False, "status": "active"})
        transcript = _write_transcript(tmp_path, [_assistant_text("wrapping up")])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(bin_dir, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_completed_run_suppressed_by_an_open_question(self, tmp_path, session):
        bin_dir = _make_work_cli(tmp_path, {"kind": "run", "completed_run": True})
        transcript = _write_transcript(tmp_path, [_assistant_text("wrapping up")])
        ask_dir = _channel_dir(tmp_path, ["000001" + QUESTION_SUFFIX])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(bin_dir, session, ask_dir))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_a_signalled_completion_stays_silent_on_every_later_stop(
        self, tmp_path, session
    ):
        """The run completed and the session signalled it. The completed-run
        fact persists for the rest of the session while the channel-dir signal
        suppressor covers only one stop, so without the transcript's
        ends-signalled state the NEXT stop would demand a second signal for the
        same completion — teaching exactly the double-signalling that degrades
        the channel."""
        bin_dir = _make_work_cli(
            tmp_path, {"kind": "run", "completed_run": True, "status": "complete"})
        transcript = _write_transcript(tmp_path, [
            _assistant_bash("work state set --status=complete", tool_id="t1"),
            _tool_result("run complete", tool_use_id="t1"),
            _assistant_bash('lmer-signal "run complete, MR !221 merged"', tool_id="t2"),
            _tool_result("signal 000001 posted", tool_use_id="t2"),
        ])
        # The signal landed in the channel dir, as it does in a real session.
        ask_dir = _channel_dir(tmp_path, ["000001" + SIGNAL_SUFFIX])
        env = _guard_env(bin_dir, session, ask_dir)

        first = _run_hook({"transcript_path": transcript}, env)
        assert first.stdout.strip() == ""
        # The one that used to block: baseline has advanced, so signal_seen is
        # False, and only the transcript's signalled state holds the facts back.
        second = _run_hook({"transcript_path": transcript}, env)
        assert second.returncode == 0
        assert second.stdout.strip() == ""
        third = _run_hook({"transcript_path": transcript}, env)
        assert third.stdout.strip() == ""

    def test_a_completion_the_session_never_signalled_still_blocks(
        self, tmp_path, session
    ):
        """The ends-signalled skip must not disarm the trigger itself: the same
        setup without the signal call still nudges."""
        bin_dir = _make_work_cli(
            tmp_path, {"kind": "run", "completed_run": True, "status": "complete"})
        transcript = _write_transcript(tmp_path, [_assistant_text("all wrapped up")])
        env = _guard_env(bin_dir, session, _channel_dir(tmp_path))
        assert json.loads(_run_hook({"transcript_path": transcript}, env).stdout)[
            "decision"] == "block"

    def test_work_failure_fails_open(self, tmp_path, session):
        bin_dir = _make_work_cli(tmp_path, {"kind": "run", "completed_run": True},
                                 exit_code=1)
        transcript = _write_transcript(tmp_path, [_assistant_text("wrapping up")])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(bin_dir, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_unparsable_resume_output_fails_open(self, tmp_path, session):
        bin_dir = _make_work_cli(tmp_path, raw="No run context for this container.")
        transcript = _write_transcript(tmp_path, [_assistant_text("wrapping up")])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(bin_dir, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_no_work_on_path_fails_open(self, tmp_path, session, empty_bin):
        transcript = _write_transcript(tmp_path, [_assistant_text("wrapping up")])
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestMainMarker:
    def test_second_stop_on_the_same_milestone_is_silent(self, tmp_path, session,
                                                         empty_bin):
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        first = _run_hook({"transcript_path": transcript}, env)
        assert json.loads(first.stdout)["decision"] == "block"
        second = _run_hook({"transcript_path": transcript}, env)
        assert second.returncode == 0
        assert second.stdout.strip() == ""

    def test_distinct_milestones_nudge_up_to_the_cap(self, tmp_path, session, empty_bin):
        env = _guard_env(empty_bin, session, _channel_dir(tmp_path))
        events = []
        outcomes = []
        for index in range(NUDGE_CAP + 1):
            events = events + [_assistant_bash("gate-push", tool_id=f"t{index}")]
            outcomes.append(
                _run_hook({"transcript_path": _write_transcript(tmp_path, events)},
                          env).stdout.strip())
        for blocked in outcomes[:NUDGE_CAP]:
            assert json.loads(blocked)["decision"] == "block"
        assert outcomes[NUDGE_CAP] == ""  # capped
        marker = json.loads(Path(MARKER_TEMPLATE.format(session=session)).read_text())
        assert marker["nudges"] == NUDGE_CAP
        assert len(marker["keys"]) == NUDGE_CAP

    def test_the_nudge_path_never_regresses_the_baseline(self, tmp_path, session,
                                                         empty_bin):
        # An empty channel dir (signals pruned, or a dir remounted) must not
        # reset a recorded baseline to null: the old signal would read as new
        # again on the next stop and suppress a real milestone.
        Path(MARKER_TEMPLATE.format(session=session)).write_text(
            json.dumps({"keys": [], "nudges": 0, "signal_baseline": 5}))
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, _channel_dir(tmp_path)))
        assert json.loads(r.stdout)["decision"] == "block"
        marker = json.loads(Path(MARKER_TEMPLATE.format(session=session)).read_text())
        assert marker["signal_baseline"] == 5

    def test_preseeded_marker_at_the_cap_is_silent(self, tmp_path, session, empty_bin):
        Path(MARKER_TEMPLATE.format(session=session)).write_text(
            json.dumps({"keys": ["a", "b", "c"], "nudges": NUDGE_CAP,
                        "signal_baseline": None}))
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_unwritable_marker_drops_the_nudge(self, tmp_path, empty_bin):
        # A session id with a slash points the marker into a directory that does
        # not exist, so the bookkeeping write raises: the hook must drop the
        # nudge rather than repeat it on every stop.
        session = f"no-such-dir-{uuid.uuid4().hex}/x"
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_corrupt_marker_drops_the_nudge(self, tmp_path, session, empty_bin):
        Path(MARKER_TEMPLATE.format(session=session)).write_text("not json{")
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestMarkerIsWrittenAtomically:
    """A half-written marker parses as corrupt, and corrupt disables the guard
    for the rest of the session (read_marker → None → fail open). Fan-out
    children share the marker path, so a torn write is reachable; the swap makes
    the state unreachable instead."""

    def test_a_failed_write_leaves_the_previous_marker_intact(self, tmp_path):
        path = tmp_path / "marker.json"
        assert write_marker(str(path), ["t1"], 1, 3, None) is True
        good = path.read_text()

        # A key json.dump cannot serialize fails mid-write on the temp file.
        assert write_marker(str(path), [{"unserializable"}], 2, 4, None) is False
        assert path.read_text() == good
        assert json.loads(path.read_text())["nudges"] == 1

    def test_a_failed_write_leaves_no_temp_file_behind(self, tmp_path):
        path = tmp_path / "marker.json"
        write_marker(str(path), [], 0, None, None)
        write_marker(str(path), [{"unserializable"}], 1, None, None)
        assert [p.name for p in tmp_path.iterdir()] == ["marker.json"]

    def test_a_successful_write_leaves_no_temp_file_behind(self, tmp_path):
        path = tmp_path / "marker.json"
        for nudges in range(3):
            assert write_marker(str(path), ["t"], nudges, nudges, nudges) is True
        assert [p.name for p in tmp_path.iterdir()] == ["marker.json"]
        assert json.loads(path.read_text()) == {
            "keys": ["t"], "nudges": 2, "signal_baseline": 2, "question_baseline": 2}

    def test_the_replace_is_within_the_target_directory(self, tmp_path):
        # A cross-filesystem rename would not be atomic, so the temp file has to
        # be a sibling of the marker.
        source = (REPO_ROOT / "hooks" / "signal_guard.py").read_text()
        assert 'tmp_path = f"{path}.{os.getpid()}.tmp"' in source
        assert "os.replace(tmp_path, path)" in source


class TestMainFailOpen:
    def test_missing_transcript_path(self, tmp_path, session, empty_bin):
        r = _run_hook({}, _guard_env(empty_bin, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_unreadable_transcript(self, tmp_path, session, empty_bin):
        r = _run_hook({"transcript_path": str(tmp_path / "nope.jsonl")},
                      _guard_env(empty_bin, session, _channel_dir(tmp_path)))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_malformed_stdin(self, tmp_path, session, empty_bin):
        r = _run_hook(None, _guard_env(empty_bin, session, _channel_dir(tmp_path)),
                      raw_stdin="not json{")
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_non_dict_stdin(self, tmp_path, session, empty_bin):
        r = _run_hook(None, _guard_env(empty_bin, session, _channel_dir(tmp_path)),
                      raw_stdin="[1, 2, 3]")
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_empty_stdin(self, tmp_path, session, empty_bin):
        r = _run_hook(None, _guard_env(empty_bin, session, _channel_dir(tmp_path)),
                      raw_stdin="")
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_ask_dir_is_a_file_not_a_directory(self, tmp_path, session, empty_bin):
        not_a_dir = tmp_path / "channel-file"
        not_a_dir.write_text("{}\n")
        transcript = _write_transcript(tmp_path, MILESTONE_TURN)
        r = _run_hook({"transcript_path": transcript},
                      _guard_env(empty_bin, session, not_a_dir))
        assert r.returncode == 0
        assert r.stdout.strip() == ""


# ---- settings.json wiring (drift guard) ---------------------------------------

class TestSettingsWiring:
    def test_stop_hook_registered_in_settings(self):
        settings = json.loads(
            (REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text())
        stop_hooks = settings.get("hooks", {}).get("Stop", [])
        commands = [
            h.get("command", "")
            for group in stop_hooks for h in group.get("hooks", [])
        ]
        assert any("signal_guard.py" in c for c in commands), (
            "Stop hook for signal_guard.py missing from settings.json")

    def test_kill_switch_is_forwarded_into_the_container(self):
        """The switch is documented as settable host-side, and the hook runs in
        the container — without this entry in cli.py's env dict the documented
        opt-out would stop at the boundary."""
        source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text()
        assert '"LMER_SIGNAL_GUARD": os.environ.get("LMER_SIGNAL_GUARD")' in source, (
            "LMER_SIGNAL_GUARD entry missing from cli.py env dict")
