"""Tests pinning the target-partitioning contract.

Contract under test
-------------------
``lmer_cli.targets.partition_targets(targets)`` splits a positional
target list into repository targets and special-target handlers:

  repo_targets, handlers = partition_targets(targets)

- ``handlers``: one ``TargetHandler`` per special target type that claimed
  at least one entry (Slack thread URLs are claimed by
  ``SlackThreadTargets``, i.e. ``is_slack_thread_url()`` returns True).
- ``repo_targets``: everything else (used as primary/secondary for repo
  clone logic, exactly as before).

Cases:
  (a) Thread-only: single Slack URL -> no normal targets, no repo clone.
  (b) Repo + Slack URL: normal list contains only the repo; Slack URL is
      recorded separately.
  (c) Slack URL anywhere in positional list is excluded from normal targets.
  (d) Multiple normal (non-Slack) targets still partition primary/secondary
      exactly as today (first = primary, rest = secondary).

The partition assertions below use the ``_partition_targets`` adapter to
flatten the handler list back into the flat slack-URL list the original
contract was written against.
"""

import os
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from slack_chat.permalink import is_slack_thread_url

from lmer_cli import targets as targets_mod
from lmer_cli.targets import (
    SlackThreadTargets,
    TARGET_HANDLER_TYPES,
    TargetHandler,
    partition_targets,
    special_target_env,
)


def _partition_targets(targets: list[str]) -> tuple[list[str], list[str]]:
    """Adapter to the original flat contract: (normal_targets, slack_urls)."""
    repo_targets, handlers = partition_targets(targets)
    slack_urls = [t for h in handlers for t in h.targets]
    return repo_targets, slack_urls

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLACK_URL = (
    "https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456"
)
SLACK_URL_2 = (
    "https://otherteam.slack.com/archives/GABCDE12345/p1600000001654321"
)
REPO_URL = "git@github.com:owner/repo.git"
REPO_URL_2 = "https://gitlab.example.com/group/project.git"
MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/42"

# ---------------------------------------------------------------------------
# Sanity: is_slack_thread_url classifies our constants correctly
# (these pass now; they confirm the fixture URLs are valid)
# ---------------------------------------------------------------------------


class TestFixtureUrlClassification:
    """Verify that the URL constants used below are classified as expected."""

    def test_slack_url_is_recognised(self):
        assert is_slack_thread_url(SLACK_URL) is True

    def test_slack_url_2_is_recognised(self):
        assert is_slack_thread_url(SLACK_URL_2) is True

    def test_repo_url_is_not_slack(self):
        assert is_slack_thread_url(REPO_URL) is False

    def test_repo_url_2_is_not_slack(self):
        assert is_slack_thread_url(REPO_URL_2) is False

    def test_mr_url_is_not_slack(self):
        assert is_slack_thread_url(MR_URL) is False


# ---------------------------------------------------------------------------
# (a) Thread-only: single Slack URL -> no normal targets
# ---------------------------------------------------------------------------


class TestPartitionSlackOnly:
    """Case (a): lmer chat <slack-url> — thread-only, no repo clone."""

    def test_normal_targets_empty(self):
        """A single Slack URL produces an empty normal-targets list."""
        normal, slack = _partition_targets([SLACK_URL])
        assert normal == []

    def test_slack_targets_contains_url(self):
        """The Slack URL is captured in slack_targets."""
        normal, slack = _partition_targets([SLACK_URL])
        assert SLACK_URL in slack

    def test_slack_targets_length_one(self):
        """Exactly one Slack target is returned."""
        normal, slack = _partition_targets([SLACK_URL])
        assert len(slack) == 1

    def test_no_repo_url_derived(self):
        """With no normal targets, primary_target is None (no repo to clone)."""
        normal, slack = _partition_targets([SLACK_URL])
        primary_target = normal[0] if normal else None
        assert primary_target is None

    def test_returns_two_sequences(self):
        """_partition_targets must return exactly two sequences."""
        result = _partition_targets([SLACK_URL])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# (b) Repo + Slack URL: repo clones, Slack target recorded separately
# ---------------------------------------------------------------------------


class TestPartitionRepoAndSlack:
    """Case (b): lmer chat <repo> <slack-url> — repo clones + Slack context."""

    def test_repo_in_normal_targets(self):
        """The repo URL appears in normal_targets."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL])
        assert REPO_URL in normal

    def test_slack_url_in_slack_targets(self):
        """The Slack URL appears in slack_targets."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL])
        assert SLACK_URL in slack

    def test_slack_url_not_in_normal_targets(self):
        """The Slack URL must NOT appear in normal_targets."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL])
        assert SLACK_URL not in normal

    def test_repo_url_not_in_slack_targets(self):
        """The repo URL must NOT appear in slack_targets."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL])
        assert REPO_URL not in slack

    def test_primary_target_is_repo(self):
        """normal_targets[0] is the repo — primary/secondary unchanged for non-Slack."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL])
        assert normal[0] == REPO_URL

    def test_normal_targets_length_one(self):
        """Exactly one normal target (the repo)."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL])
        assert len(normal) == 1

    def test_slack_targets_length_one(self):
        """Exactly one Slack target."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL])
        assert len(slack) == 1

    def test_order_repo_slack_vs_slack_repo(self):
        """Partition is order-independent for classification."""
        normal_a, slack_a = _partition_targets([REPO_URL, SLACK_URL])
        normal_b, slack_b = _partition_targets([SLACK_URL, REPO_URL])
        # Both orderings produce the same sets (order within each list may vary)
        assert set(normal_a) == set(normal_b)
        assert set(slack_a) == set(slack_b)


# ---------------------------------------------------------------------------
# (c) Slack URL anywhere is excluded from primary/secondary clone selection
# ---------------------------------------------------------------------------


class TestSlackExcludedFromCloneSelection:
    """Case (c): Slack URL at any position is stripped from normal_targets."""

    def test_slack_first_excluded(self):
        """Slack URL at position 0 is not in normal_targets."""
        normal, slack = _partition_targets([SLACK_URL, REPO_URL])
        assert SLACK_URL not in normal

    def test_slack_last_excluded(self):
        """Slack URL at last position is not in normal_targets."""
        normal, slack = _partition_targets([REPO_URL, REPO_URL_2, SLACK_URL])
        assert SLACK_URL not in normal

    def test_slack_middle_excluded(self):
        """Slack URL in the middle is not in normal_targets."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL, REPO_URL_2])
        assert SLACK_URL not in normal

    def test_normal_targets_preserve_non_slack(self):
        """All non-Slack targets are present in normal_targets regardless of Slack position."""
        normal, slack = _partition_targets([REPO_URL, SLACK_URL, REPO_URL_2])
        assert REPO_URL in normal
        assert REPO_URL_2 in normal

    def test_multiple_slack_urls_all_excluded(self):
        """Multiple Slack URLs in the list are all excluded from normal_targets."""
        normal, slack = _partition_targets([SLACK_URL, REPO_URL, SLACK_URL_2])
        assert SLACK_URL not in normal
        assert SLACK_URL_2 not in normal

    def test_multiple_slack_urls_all_captured(self):
        """All Slack URLs appear in slack_targets."""
        normal, slack = _partition_targets([SLACK_URL, REPO_URL, SLACK_URL_2])
        assert SLACK_URL in slack
        assert SLACK_URL_2 in slack

    def test_mr_url_treated_as_normal(self):
        """A GitLab MR URL is NOT a Slack target — stays in normal_targets."""
        normal, slack = _partition_targets([MR_URL, SLACK_URL])
        assert MR_URL in normal
        assert SLACK_URL not in normal


# ---------------------------------------------------------------------------
# (d) Multiple normal targets still partition primary/secondary as today
# ---------------------------------------------------------------------------


class TestMultipleNormalTargetsPrimarySecondary:
    """Case (d): No Slack URL — primary/secondary semantics unchanged."""

    def test_single_normal_primary(self):
        """One normal target -> primary = targets[0], no secondary."""
        normal, slack = _partition_targets([REPO_URL])
        assert normal[0] == REPO_URL
        assert slack == []

    def test_two_normal_primary_secondary(self):
        """Two normal targets -> first is primary, second is secondary."""
        normal, slack = _partition_targets([REPO_URL, REPO_URL_2])
        assert normal[0] == REPO_URL
        assert normal[1] == REPO_URL_2

    def test_two_normal_secondary_list(self):
        """secondary_targets = normal[1:] exactly as today."""
        normal, slack = _partition_targets([REPO_URL, REPO_URL_2, MR_URL])
        secondary_targets = normal[1:]
        assert secondary_targets == [REPO_URL_2, MR_URL]

    def test_empty_targets(self):
        """Empty target list -> both sequences empty."""
        normal, slack = _partition_targets([])
        assert normal == []
        assert slack == []

    def test_no_slack_urls_empty_slack_list(self):
        """All non-Slack targets -> slack_targets is empty."""
        normal, slack = _partition_targets([REPO_URL, REPO_URL_2])
        assert slack == []

    def test_normal_targets_order_preserved(self):
        """Order of normal targets is preserved (primary/secondary stable)."""
        targets = [REPO_URL, REPO_URL_2, MR_URL]
        normal, slack = _partition_targets(targets)
        assert normal == [REPO_URL, REPO_URL_2, MR_URL]

    def test_normal_targets_with_slack_interspersed_order_preserved(self):
        """Non-Slack targets keep their relative order when Slack is mixed in."""
        targets = [REPO_URL, SLACK_URL, REPO_URL_2, SLACK_URL_2, MR_URL]
        normal, slack = _partition_targets(targets)
        # REPO_URL appears before REPO_URL_2 appears before MR_URL
        assert normal.index(REPO_URL) < normal.index(REPO_URL_2)
        assert normal.index(REPO_URL_2) < normal.index(MR_URL)


# ---------------------------------------------------------------------------
# Handler-level contract (lmer_cli.targets) — the seam each special target
# type plugs into: registry, per-type state, repo-less policy, container env.
# ---------------------------------------------------------------------------


class TestTargetHandlerContract:
    """Direct coverage of the special-target-handler seam."""

    def test_slack_registered_in_handler_types(self):
        """SlackThreadTargets is a registered special target type."""
        assert SlackThreadTargets in TARGET_HANDLER_TYPES

    def test_partition_returns_slack_handler_instance(self):
        """A Slack URL yields exactly one SlackThreadTargets handler."""
        _, handlers = partition_targets([SLACK_URL])
        assert len(handlers) == 1
        assert isinstance(handlers[0], SlackThreadTargets)

    def test_no_handlers_without_special_targets(self):
        """Repo-only target lists produce no handlers."""
        _, handlers = partition_targets([REPO_URL])
        assert handlers == []

    def test_first_slack_target_drives_chat_context(self):
        """Only the first Slack URL is parsed into channel/thread_ts."""
        _, handlers = partition_targets([SLACK_URL, SLACK_URL_2])
        handler = handlers[0]
        assert handler.targets == [SLACK_URL, SLACK_URL_2]
        assert handler.permalink == SLACK_URL
        assert handler.channel == "C0123ABCDEF"
        assert handler.thread_ts == "1700000000.123456"

    def test_repoless_allowed_for_chat_only(self):
        """Repo-less sessions are gated to the chat task."""
        handler = SlackThreadTargets()
        assert handler.supports_repoless_session("chat") is True
        assert handler.supports_repoless_session("review") is False
        assert handler.supports_repoless_session(None) is False

    def test_repoless_unsupported_reason_names_chat_and_task(self):
        """The refusal message names the chat-only constraint and the task."""
        reason = SlackThreadTargets().repoless_unsupported_reason("review")
        assert "'chat'" in reason
        assert "'review'" in reason

    def test_validate_environment_requires_bot_token(self):
        """Missing SLACK_BOT_TOKEN produces an error naming the variable."""
        handler = SlackThreadTargets()
        with patch.dict(os.environ):
            os.environ.pop("SLACK_BOT_TOKEN", None)
            message = handler.validate_environment()
        assert message is not None
        assert "SLACK_BOT_TOKEN" in message

    def test_validate_environment_passes_with_bot_token(self):
        """A set SLACK_BOT_TOKEN validates cleanly."""
        handler = SlackThreadTargets()
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"}):
            assert handler.validate_environment() is None

    def test_special_target_env_seeds_all_keys_none_when_inactive(self):
        """Without handlers, every registered env key is present and None.

        Key-presence is what blocks the CLI's .env merge from forwarding
        e.g. SLACK_BOT_TOKEN into sessions without a Slack target.
        """
        env = special_target_env([])
        for handler_cls in TARGET_HANDLER_TYPES:
            for key in handler_cls.container_env_keys:
                assert key in env
                assert env[key] is None

    def test_special_target_env_fills_slack_context(self):
        """An active Slack handler contributes token + parsed thread context."""
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"}):
            _, handlers = partition_targets([SLACK_URL])
            env = special_target_env(handlers)
        assert env["SLACK_BOT_TOKEN"] == "xoxb-test"
        assert env["LMER_SLACK_CHANNEL"] == "C0123ABCDEF"
        assert env["LMER_SLACK_THREAD_TS"] == "1700000000.123456"
        assert env["LMER_SLACK_PERMALINK"] == SLACK_URL

    def test_default_handler_refuses_repoless_cleanly(self):
        """A subclass that doesn't opt into repo-less must refuse with a
        clean message (no NotImplementedError) when its target is the only
        one outside a git checkout."""

        class MinimalTargets(TargetHandler):
            @classmethod
            def matches(cls, target):
                return False

        handler = MinimalTargets()
        assert handler.supports_repoless_session("chat") is False
        reason = handler.repoless_unsupported_reason("chat")
        assert "repository target" in reason
        assert "'chat'" in reason


# ---------------------------------------------------------------------------
# Credential + container-env contract tests (behavioral, through main())
#
# Contract under test
# -------------------
# (a) Slack target present + SLACK_BOT_TOKEN absent -> CLI exits non-zero with
#     a message naming both ".env" and "SLACK_BOT_TOKEN".
# (b) SLACK_BOT_TOKEN present -> forwarded into container env dict.
#     SLACK_APP_TOKEN forwarded if set; absence is NOT fatal.
# (c) Detected Slack target -> LMER_SLACK_CHANNEL, LMER_SLACK_THREAD_TS,
#     LMER_SLACK_PERMALINK injected into container env from parse_slack_permalink.
# (d) --show-env redaction (existing TOKEN|KEY|SECRET rule) masks both tokens.
# ---------------------------------------------------------------------------

# Minimal env for calling main(): provides only what's needed to reach the
# credential-check gate without triggering unrelated failures.  Tests supply
# SLACK_* on top as needed.  LMER_IMAGE bypasses image resolution so we don't
# need a real container runtime.  LMER_WORK_REPO satisfies the work-repo
# requirement so the test gets past unrelated early-exit gates.
_BASE_ENV = {
    "HOME": os.path.expanduser("~"),
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "LMER_IMAGE": "lmer-test-image:latest",
    "LMER_WORK_REPO": "https://github.com/example/work-repo.git",
}

# Slack permalink constant (reuse from above)
_SLACK_URL = SLACK_URL  # https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456
_EXPECTED_CHANNEL = "C0123ABCDEF"
_EXPECTED_THREAD_TS = "1700000000.123456"


def _make_main_mocks(captured_env: dict | None = None):
    """Return a context-manager stack of patches sufficient to let main() run
    without a real container runtime.  Patches are applied in the caller via
    ``unittest.mock.patch`` calls so the test controls what is returned.

    ``captured_env`` — if provided, the mock for ``env_args`` stores the env
    dict that main() passes into it so tests can inspect the container env.
    """
    from contextlib import ExitStack
    from unittest.mock import MagicMock, patch

    stack = ExitStack()

    # --- runtime / image ---
    stack.enter_context(
        patch("lmer_cli.cli.detect_runtime", return_value="podman")
    )
    stack.enter_context(
        patch("lmer_cli.cli.ensure_image", return_value=True)
    )
    stack.enter_context(
        patch("lmer_cli.cli.base_run_args", return_value=[])
    )

    # --- mounts (return empty lists so run[] stays clean) ---
    stack.enter_context(
        patch("lmer_cli.cli.build_global_mount", return_value=[])
    )
    stack.enter_context(
        patch("lmer_cli.cli.build_lmer_docs_mount", return_value=[])
    )
    stack.enter_context(
        patch("lmer_cli.cli.build_container_home_mounts", return_value=[])
    )
    stack.enter_context(
        patch(
            "lmer_cli.cli.build_user_mounts",
            return_value=([], False),
        )
    )
    stack.enter_context(
        patch("lmer_cli.cli.build_workspace_mount", return_value=[])
    )
    stack.enter_context(
        patch("lmer_cli.cli.build_checkout_mount", return_value=[])
    )
    stack.enter_context(
        patch("lmer_cli.cli.build_host_repo_ro_mount", return_value=[])
    )
    stack.enter_context(
        patch("lmer_cli.cli.build_host_uv_cache_mount", return_value=[])
    )
    stack.enter_context(
        patch("lmer_cli.cli.build_external_taskdef_mounts", return_value=[])
    )
    stack.enter_context(
        patch("lmer_cli.cli.ensure_container_home", return_value="/tmp/fake-home")
    )
    stack.enter_context(
        patch("lmer_cli.cli._check_ssh_setup", return_value=None)
    )

    # --- repo resolution (returns a placeholder so Slack-only path is ok) ---
    stack.enter_context(
        patch(
            "lmer_cli.cli.resolve.normalize_repo_url",
            return_value=("https://github.com/owner/repo.git", None),
        )
    )

    # --- env_args: optionally capture what the CLI hands in ---
    if captured_env is not None:

        def _capture_env_args(env):
            captured_env.update(env)
            return []

        stack.enter_context(
            patch("lmer_cli.cli.env_args", side_effect=_capture_env_args)
        )
    else:
        stack.enter_context(patch("lmer_cli.cli.env_args", return_value=[]))

    # --- subprocess.call: prevent actually launching a container ---
    stack.enter_context(
        patch("lmer_cli.cli.subprocess.call", return_value=0)
    )

    return stack


# ---------------------------------------------------------------------------
# (a) Slack target + no SLACK_BOT_TOKEN → fast-fail, non-zero, message names
#     both ".env" and "SLACK_BOT_TOKEN"
# ---------------------------------------------------------------------------


class TestSlackCredentialFastFail:
    """Case (a): SLACK_BOT_TOKEN absent with a Slack target -> CLI fails fast."""

    def test_exits_nonzero_when_bot_token_absent(self, capsys):
        """main() must return non-zero when a Slack URL is given but
        SLACK_BOT_TOKEN is not in the environment."""
        with patch.dict(os.environ, _BASE_ENV, clear=True):
            with _make_main_mocks():
                from lmer_cli.cli import main

                rc = main(["--no-task", "--exec", "true", _SLACK_URL])

        assert rc != 0, (
            "Expected non-zero exit when SLACK_BOT_TOKEN is absent and a Slack "
            f"URL is in targets; got rc={rc}"
        )

    def test_error_message_names_slack_bot_token(self, capsys):
        """The error output must mention SLACK_BOT_TOKEN by name."""
        with patch.dict(os.environ, _BASE_ENV, clear=True):
            with _make_main_mocks():
                from lmer_cli.cli import main

                main(["--no-task", "--exec", "true", _SLACK_URL])

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "SLACK_BOT_TOKEN" in combined, (
            f"Expected 'SLACK_BOT_TOKEN' in CLI output; got: {combined!r}"
        )

    def test_error_message_names_dotenv_file(self, capsys):
        """The error output must tell the user to set the token in .env."""
        with patch.dict(os.environ, _BASE_ENV, clear=True):
            with _make_main_mocks():
                from lmer_cli.cli import main

                main(["--no-task", "--exec", "true", _SLACK_URL])

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert ".env" in combined, (
            f"Expected '.env' in CLI output to guide the user; got: {combined!r}"
        )

    def test_no_container_launch_when_bot_token_absent(self, capsys):
        """subprocess.call must NOT be invoked when SLACK_BOT_TOKEN is absent."""
        from unittest.mock import patch as mpatch

        call_spy = []
        with patch.dict(os.environ, _BASE_ENV, clear=True):
            with _make_main_mocks():
                with mpatch(
                    "lmer_cli.cli.subprocess.call",
                    side_effect=lambda *a, **k: call_spy.append(a) or 0,
                ):
                    from lmer_cli.cli import main

                    rc = main(["--no-task", "--exec", "true", _SLACK_URL])

        assert rc != 0 or call_spy == [], (
            "CLI must either exit non-zero OR skip subprocess.call when "
            "SLACK_BOT_TOKEN is absent; container was launched unexpectedly."
        )


# ---------------------------------------------------------------------------
# (b) SLACK_BOT_TOKEN present -> forwarded into container env;
#     SLACK_APP_TOKEN optional (absence not fatal)
# ---------------------------------------------------------------------------


class TestSlackTokenContainerEnv:
    """Case (b): SLACK_BOT_TOKEN flows into container env dict."""

    def test_bot_token_in_container_env(self):
        """SLACK_BOT_TOKEN from host env must appear in the container env dict."""
        env_in = {**_BASE_ENV, "SLACK_BOT_TOKEN": "xoxb-test-bot-token"}
        captured = {}
        with patch.dict(os.environ, env_in, clear=True):
            with _make_main_mocks(captured_env=captured):
                from lmer_cli.cli import main

                main(["--no-task", "--exec", "true", _SLACK_URL])

        assert "SLACK_BOT_TOKEN" in captured, (
            "SLACK_BOT_TOKEN must be forwarded into the container env dict "
            f"when set; container env keys: {sorted(captured.keys())}"
        )
        assert captured["SLACK_BOT_TOKEN"] == "xoxb-test-bot-token"

    def test_app_token_in_container_env_when_set(self):
        """SLACK_APP_TOKEN from host env must appear in the container env dict
        when it is set."""
        env_in = {
            **_BASE_ENV,
            "SLACK_BOT_TOKEN": "xoxb-test-bot-token",
            "SLACK_APP_TOKEN": "xapp-test-app-token",
        }
        captured = {}
        with patch.dict(os.environ, env_in, clear=True):
            with _make_main_mocks(captured_env=captured):
                from lmer_cli.cli import main

                main(["--no-task", "--exec", "true", _SLACK_URL])

        assert "SLACK_APP_TOKEN" in captured, (
            "SLACK_APP_TOKEN must be forwarded into container env when set; "
            f"container env keys: {sorted(captured.keys())}"
        )
        assert captured["SLACK_APP_TOKEN"] == "xapp-test-app-token"

    def test_absence_of_app_token_is_not_fatal(self, capsys):
        """main() must succeed (rc == 0) when SLACK_BOT_TOKEN is set but
        SLACK_APP_TOKEN is absent — SLACK_APP_TOKEN is optional."""
        env_in = {**_BASE_ENV, "SLACK_BOT_TOKEN": "xoxb-test-bot-token"}
        with patch.dict(os.environ, env_in, clear=True):
            with _make_main_mocks():
                from lmer_cli.cli import main

                rc = main(["--no-task", "--exec", "true", _SLACK_URL])

        assert rc == 0, (
            "main() must not fail when SLACK_APP_TOKEN is absent and "
            f"SLACK_BOT_TOKEN is set; rc={rc}"
        )

    def test_app_token_absent_means_key_absent_or_none_in_env(self):
        """When SLACK_APP_TOKEN is not in host env, it must not appear in the
        container env dict with a non-None value (key may be absent or None)."""
        env_in = {**_BASE_ENV, "SLACK_BOT_TOKEN": "xoxb-test-bot-token"}
        captured = {}
        with patch.dict(os.environ, env_in, clear=True):
            with _make_main_mocks(captured_env=captured):
                from lmer_cli.cli import main

                main(["--no-task", "--exec", "true", _SLACK_URL])

        app_token_val = captured.get("SLACK_APP_TOKEN")
        assert app_token_val is None, (
            "SLACK_APP_TOKEN must not appear with a non-None value in container "
            f"env when the host env lacks it; got: {app_token_val!r}"
        )


# ---------------------------------------------------------------------------
# (c) Detected Slack target -> LMER_SLACK_CHANNEL, LMER_SLACK_THREAD_TS,
#     LMER_SLACK_PERMALINK set in container env from parse_slack_permalink
# ---------------------------------------------------------------------------


class TestSlackPermalinkContainerEnv:
    """Case (c): Slack permalink parsed and injected into container env."""

    def _run_with_slack(self):
        """Helper: run main() with SLACK_BOT_TOKEN set, return captured env."""
        env_in = {**_BASE_ENV, "SLACK_BOT_TOKEN": "xoxb-test-bot-token"}
        captured: dict = {}
        with patch.dict(os.environ, env_in, clear=True):
            with _make_main_mocks(captured_env=captured):
                from lmer_cli.cli import main

                main(["--no-task", "--exec", "true", _SLACK_URL])
        return captured

    def test_lmer_slack_channel_in_container_env(self):
        """LMER_SLACK_CHANNEL must be set to the channel ID parsed from the URL."""
        captured = self._run_with_slack()
        assert "LMER_SLACK_CHANNEL" in captured, (
            "LMER_SLACK_CHANNEL missing from container env; "
            f"keys: {sorted(captured.keys())}"
        )
        assert captured["LMER_SLACK_CHANNEL"] == _EXPECTED_CHANNEL, (
            f"Expected channel {_EXPECTED_CHANNEL!r}, "
            f"got {captured.get('LMER_SLACK_CHANNEL')!r}"
        )

    def test_lmer_slack_thread_ts_in_container_env(self):
        """LMER_SLACK_THREAD_TS must be the dotted timestamp from the URL."""
        captured = self._run_with_slack()
        assert "LMER_SLACK_THREAD_TS" in captured, (
            "LMER_SLACK_THREAD_TS missing from container env; "
            f"keys: {sorted(captured.keys())}"
        )
        assert captured["LMER_SLACK_THREAD_TS"] == _EXPECTED_THREAD_TS, (
            f"Expected ts {_EXPECTED_THREAD_TS!r}, "
            f"got {captured.get('LMER_SLACK_THREAD_TS')!r}"
        )

    def test_lmer_slack_permalink_in_container_env(self):
        """LMER_SLACK_PERMALINK must be the original Slack URL."""
        captured = self._run_with_slack()
        assert "LMER_SLACK_PERMALINK" in captured, (
            "LMER_SLACK_PERMALINK missing from container env; "
            f"keys: {sorted(captured.keys())}"
        )
        assert captured["LMER_SLACK_PERMALINK"] == _SLACK_URL, (
            f"Expected permalink {_SLACK_URL!r}, "
            f"got {captured.get('LMER_SLACK_PERMALINK')!r}"
        )

    def test_slack_env_vars_absent_when_no_slack_target(self):
        """LMER_SLACK_* keys must not appear (or be None) when no Slack URL is
        given — they should only be injected when a Slack thread is targeted."""
        env_in = {**_BASE_ENV}
        captured: dict = {}
        with patch.dict(os.environ, env_in, clear=True):
            with _make_main_mocks(captured_env=captured):
                from lmer_cli.cli import main

                main(["--no-task", "--exec", "true", REPO_URL])

        for key in ("LMER_SLACK_CHANNEL", "LMER_SLACK_THREAD_TS", "LMER_SLACK_PERMALINK"):
            val = captured.get(key)
            assert val is None, (
                f"{key} must be absent or None in container env when no Slack "
                f"URL is given; got {val!r}"
            )


# ---------------------------------------------------------------------------
# (d) --show-env redaction: TOKEN|KEY|SECRET pattern masks Slack tokens
# ---------------------------------------------------------------------------


class TestSlackTokenRedaction:
    """Case (d): _redact_env_value masks SLACK_BOT_TOKEN and SLACK_APP_TOKEN."""

    def test_bot_token_is_redacted(self):
        """SLACK_BOT_TOKEN value must be masked by _redact_env_value (TOKEN pattern)."""
        from lmer_cli.cli import _redact_env_value

        result = _redact_env_value("SLACK_BOT_TOKEN", "xoxb-abc123def456")
        assert "xoxb-abc123def456" not in result, (
            f"SLACK_BOT_TOKEN value must be redacted; got {result!r}"
        )
        assert result.endswith("***"), (
            f"Redacted value must end with '***'; got {result!r}"
        )

    def test_app_token_is_redacted(self):
        """SLACK_APP_TOKEN value must be masked by _redact_env_value (TOKEN pattern)."""
        from lmer_cli.cli import _redact_env_value

        result = _redact_env_value("SLACK_APP_TOKEN", "xapp-abc123def456")
        assert "xapp-abc123def456" not in result, (
            f"SLACK_APP_TOKEN value must be redacted; got {result!r}"
        )
        assert result.endswith("***"), (
            f"Redacted value must end with '***'; got {result!r}"
        )

    def test_bot_token_shows_prefix_hint(self):
        """SLACK_BOT_TOKEN redaction shows the first 4 chars as a hint (>4 char value)."""
        from lmer_cli.cli import _redact_env_value

        result = _redact_env_value("SLACK_BOT_TOKEN", "xoxb-abc123def456")
        assert result.startswith("xoxb"), (
            f"Expected first 4 chars 'xoxb' as hint; got {result!r}"
        )

    def test_app_token_shows_prefix_hint(self):
        """SLACK_APP_TOKEN redaction shows the first 4 chars as a hint (>4 char value)."""
        from lmer_cli.cli import _redact_env_value

        result = _redact_env_value("SLACK_APP_TOKEN", "xapp-abc123def456")
        assert result.startswith("xapp"), (
            f"Expected first 4 chars 'xapp' as hint; got {result!r}"
        )

    def test_short_bot_token_fully_masked(self):
        """A very short SLACK_BOT_TOKEN (<=4 chars) is fully replaced with '***'."""
        from lmer_cli.cli import _redact_env_value

        result = _redact_env_value("SLACK_BOT_TOKEN", "abc")
        assert result == "***", (
            f"Short token must be fully masked as '***'; got {result!r}"
        )


# ---------------------------------------------------------------------------
# (e) Slack-only target without an inferrable repo -> repo-less session
#     (MR 86 follow-up: "providing just a slack thread as a target should work")
# ---------------------------------------------------------------------------


def _run_main_repoless(argv, captured_env=None, resolve_fails=True):
    """Run main() with the standard mock stack; optionally make repo
    resolution raise ResolveError (simulating a non-git cwd)."""
    from lmer_cli import resolve as resolve_mod

    env_in = {**_BASE_ENV, "SLACK_BOT_TOKEN": "xoxb-test-bot-token"}
    with patch.dict(os.environ, env_in, clear=True):
        with _make_main_mocks(captured_env=captured_env):
            ctx = (
                patch(
                    "lmer_cli.cli.resolve.normalize_repo_url",
                    side_effect=resolve_mod.ResolveError(
                        "No <target> provided and could not infer git "
                        "origin from current directory"
                    ),
                )
                if resolve_fails
                else nullcontext()
            )
            with ctx:
                from lmer_cli.cli import main

                return main(argv)


class TestSlackOnlyNoRepo:
    """A Slack thread permalink as the sole target must start a repo-less
    session when no git origin can be inferred from cwd, instead of failing
    with 'No <target> provided'."""

    def _run_main(self, argv, captured_env=None, resolve_fails=True):
        return _run_main_repoless(argv, captured_env, resolve_fails)

    def test_slack_only_target_succeeds_without_repo(self):
        """main() must return 0 for `lmer chat <slack-url>` from a non-git cwd."""
        rc = self._run_main(["chat", _SLACK_URL])
        assert rc == 0, (
            "Slack-only target with no inferrable git origin must start a "
            f"repo-less session, not fail; rc={rc}"
        )

    def test_slack_only_no_repo_sets_lmer_no_repo(self):
        """Container env must carry LMER_NO_REPO=1 in the repo-less case."""
        captured: dict = {}
        self._run_main(["chat", _SLACK_URL], captured_env=captured)
        assert captured.get("LMER_NO_REPO") == "1", (
            "LMER_NO_REPO must be '1' for a Slack-only repo-less session; "
            f"got {captured.get('LMER_NO_REPO')!r}"
        )

    def test_slack_only_no_repo_leaves_repo_url_unset(self):
        """LMER_REPO_URL must be None/absent in the repo-less case."""
        captured: dict = {}
        self._run_main(["chat", _SLACK_URL], captured_env=captured)
        assert captured.get("LMER_REPO_URL") is None, (
            "LMER_REPO_URL must be unset for a repo-less session; "
            f"got {captured.get('LMER_REPO_URL')!r}"
        )

    def test_slack_only_no_repo_still_sets_slack_env(self):
        """Slack channel/thread env injection must still happen without a repo."""
        captured: dict = {}
        self._run_main(["chat", _SLACK_URL], captured_env=captured)
        assert captured.get("LMER_SLACK_CHANNEL") == _EXPECTED_CHANNEL
        assert captured.get("LMER_SLACK_THREAD_TS") == _EXPECTED_THREAD_TS
        assert captured.get("LMER_SLACK_PERMALINK") == _SLACK_URL

    def test_slack_only_with_inferrable_repo_keeps_repo(self):
        """When cwd inference succeeds, the session keeps the inferred repo
        and LMER_NO_REPO stays unset."""
        captured: dict = {}
        self._run_main(
            ["chat", _SLACK_URL], captured_env=captured, resolve_fails=False
        )
        assert captured.get("LMER_REPO_URL"), (
            "Inferred repo URL must be used when cwd is a git repository"
        )
        assert captured.get("LMER_NO_REPO") is None, (
            "LMER_NO_REPO must stay unset when a repo was inferred; "
            f"got {captured.get('LMER_NO_REPO')!r}"
        )

    def test_normal_target_resolve_failure_still_fatal(self):
        """A failing resolve with a NORMAL target present must still exit
        non-zero — the repo-less path applies only to Slack-only targets."""
        rc = self._run_main(["chat", "./not-a-repo", _SLACK_URL])
        assert rc != 0, (
            "ResolveError with a normal target present must remain fatal; "
            f"rc={rc}"
        )

    def test_non_chat_task_slack_only_still_fatal(self, capsys):
        """The repo-less fallback is gated to the chat task: any other task
        with a Slack-only target and no inferrable repo must fail fast with
        an error naming the chat-only constraint."""
        rc = self._run_main(["review", _SLACK_URL])
        assert rc != 0, (
            "Slack-only target must remain fatal for non-chat tasks; "
            f"rc={rc}"
        )
        cap = capsys.readouterr()
        combined = cap.out + cap.err
        assert "chat" in combined, (
            f"Error message should point at the chat-only constraint; got: {combined!r}"
        )

    def test_non_chat_task_slack_only_does_not_set_no_repo(self):
        """No container launch / no LMER_NO_REPO for a non-chat repo-less
        attempt."""
        captured: dict = {}
        self._run_main(["develop", _SLACK_URL], captured_env=captured)
        assert captured.get("LMER_NO_REPO") is None, (
            "LMER_NO_REPO must not be set for non-chat tasks; "
            f"got {captured.get('LMER_NO_REPO')!r}"
        )


# ---------------------------------------------------------------------------
# (f) Multi-type repo-less agreement: when several special target types are
#     claimed and no repo target is given, every handler must support a
#     repo-less session for the task — one refusing handler vetoes it.
# ---------------------------------------------------------------------------


class _DummyNoRepolessTargets(TargetHandler):
    """Test-only second target type that never supports repo-less."""

    @classmethod
    def matches(cls, target):
        return target.startswith("dummy://")


class _DummyRepolessChatTargets(TargetHandler):
    """Test-only second target type that allows repo-less chat sessions."""

    repoless_task_ids = frozenset({"chat"})

    @classmethod
    def matches(cls, target):
        return target.startswith("dummy://")

    def repoless_start_message(self):
        return "dummy repo-less session"


class TestMultiTypeRepolessAgreement:
    """Repo-less sessions require agreement from every claimed handler."""

    _DUMMY_URL = "dummy://some-thing"

    def test_refusing_handler_vetoes_repoless(self, capsys):
        """A second type that doesn't support repo-less makes the mixed
        special-only invocation fail fast with its refusal reason."""
        registry = (SlackThreadTargets, _DummyNoRepolessTargets)
        with patch.object(targets_mod, "TARGET_HANDLER_TYPES", registry):
            rc = _run_main_repoless(["chat", _SLACK_URL, self._DUMMY_URL])
        assert rc != 0, (
            f"A refusing handler must veto the repo-less session; rc={rc}"
        )
        cap = capsys.readouterr()
        combined = cap.out + cap.err
        assert "repository target" in combined, (
            f"Error should carry the refusing handler's reason; got: {combined!r}"
        )

    def test_all_supporting_handlers_allow_repoless(self):
        """When every claimed handler supports repo-less for the task, the
        session starts repo-less exactly as in the single-type case."""
        registry = (SlackThreadTargets, _DummyRepolessChatTargets)
        captured: dict = {}
        with patch.object(targets_mod, "TARGET_HANDLER_TYPES", registry):
            rc = _run_main_repoless(
                ["chat", _SLACK_URL, self._DUMMY_URL], captured_env=captured
            )
        assert rc == 0, f"All-supporting handlers must allow repo-less; rc={rc}"
        assert captured.get("LMER_NO_REPO") == "1"
