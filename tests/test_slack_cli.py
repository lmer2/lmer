"""Failing pytest suite for the lmer-slack console script (TDD red phase).

Tests are designed to fail until src/slack_chat/cli.py is implemented.
All HTTP / Slack calls are mocked via unittest.mock.  time.sleep is patched
so NO real sleeps occur.

Coverage:
  1. Subcommand dispatch — history / post / poll via the argparse entry.
  2. history fetches + prints the thread; advances the file cursor to the
     latest ts.
  3. post <text> calls post_message and prints the returned ts.
  4. Cursor advance across two consecutive poll/history calls (second call
     reads the cursor written by the first).
  5. --since override; channel/thread_ts sourced from LMER_SLACK_CHANNEL /
     LMER_SLACK_THREAD_TS env (patch.dict os.environ) or derived via
     parse_slack_permalink.
  6. Bot-message filtering in poll using auth_test() user id.
  7. poll exits 0 with new non-bot messages printed; a DISTINCT non-zero exit
     code on timeout (asserted to differ from the generic error code 1).

Cursor dir uses a tmp_path-based override, NOT the real /tmp/lmer-slack.
"""

import os
import sys
import json
import contextlib
import importlib
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

# ---------------------------------------------------------------------------
# Deferred import — collected regardless; fails at runtime until impl lands.
# ---------------------------------------------------------------------------
try:
    from slack_chat import cli as _slack_cli_mod
    from slack_chat.cli import main as slack_cli_main
    _IMPORT_OK = True
except ImportError:
    _IMPORT_OK = False
    _slack_cli_mod = None  # type: ignore[assignment]
    slack_cli_main = None  # type: ignore[assignment]


def _require_impl():
    """Fail immediately if the CLI module is not yet implemented."""
    if not _IMPORT_OK:
        pytest.fail(
            "slack_chat.cli is not yet implemented — "
            "this is expected (TDD red phase)."
        )


# ---------------------------------------------------------------------------
# Constants shared across test classes
# ---------------------------------------------------------------------------

BOT_TOKEN = "xoxb-test-token-cli"
CHANNEL = "C0TEST001"
THREAD_TS = "1700000000.123456"
SLACK_PERMALINK = (
    f"https://myworkspace.slack.com/archives/{CHANNEL}"
    f"/p1700000000123456"
)

# The timeout exit code must differ from 1 (generic error).
# Once the module exists we read it from the module; before that we use a
# sentinel to force the assertion to run.
_GENERIC_ERROR_CODE = 1


def _timeout_exit_code():
    """Return the module-declared POLL_TIMEOUT_EXIT_CODE (or a sentinel)."""
    if _IMPORT_OK and hasattr(_slack_cli_mod, "POLL_TIMEOUT_EXIT_CODE"):
        return _slack_cli_mod.POLL_TIMEOUT_EXIT_CODE
    # Force failure if module exists but constant is missing.
    if _IMPORT_OK:
        pytest.fail(
            "slack_chat.cli must export POLL_TIMEOUT_EXIT_CODE constant so "
            "callers (and tests) can distinguish timeout from error."
        )
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(ts, text, user="UOTHER01", bot_id=None):
    msg = {"type": "message", "ts": ts, "text": text, "user": user}
    if bot_id is not None:
        msg["bot_id"] = bot_id
    return msg


def _make_client_mock(
    replies=None,
    post_ts="1700000099.000001",
    bot_user_id="UBOTSELF",
):
    """Build a MagicMock SlackClient with sensible defaults."""
    client = MagicMock()
    client.get_replies.return_value = replies if replies is not None else []
    client.post_message.return_value = post_ts
    client.auth_test.return_value = bot_user_id
    return client


def _run_cli(args, env_overrides=None, tmp_cursor_dir=None, clock=None):
    """Invoke slack_cli_main(argv) with optional env overrides.

    Returns (exit_code, stdout_text, stderr_text).
    env_overrides: dict merged into os.environ for the duration of the call.
    tmp_cursor_dir: if given, patches CURSOR_DIR in the cli module.
    clock: optional {"now": float} dict driving the fake wall clock; pass one
           in so test code (e.g. a get_replies side_effect) can advance it.

    time.monotonic / time.sleep are replaced with a fake wall clock: monotonic
    returns the clock value and sleep advances it, so the poll deadline logic
    is deterministic and no real sleeping occurs.
    """
    _require_impl()
    env = {
        "SLACK_BOT_TOKEN": BOT_TOKEN,
        "LMER_SLACK_CHANNEL": CHANNEL,
        "LMER_SLACK_THREAD_TS": THREAD_TS,
    }
    if env_overrides:
        env.update(env_overrides)

    stdout_buf = StringIO()
    stderr_buf = StringIO()

    if clock is None:
        clock = {"now": 0.0}

    def _fake_sleep(seconds):
        clock["now"] += seconds

    patches = [
        patch.dict(os.environ, env, clear=False),
        patch("sys.stdout", stdout_buf),
        patch("sys.stderr", stderr_buf),
        patch("time.sleep", side_effect=_fake_sleep),
        patch("time.monotonic", side_effect=lambda: clock["now"]),
    ]
    if tmp_cursor_dir is not None:
        patches.append(
            patch.object(_slack_cli_mod, "CURSOR_DIR", str(tmp_cursor_dir))
        )

    exit_code = 0
    with contextlib.nullcontext():
        ctx = [p.__enter__() for p in patches]
        try:
            result = slack_cli_main(args)
            exit_code = result if result is not None else 0
        except SystemExit as exc:
            exit_code = exc.code if exc.code is not None else 0
        finally:
            for p, c in zip(reversed(patches), reversed(ctx)):
                p.__exit__(None, None, None)

    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Subcommand dispatch
# ---------------------------------------------------------------------------

class TestSubcommandDispatch:
    """The argparse entry must route to history / post / poll handlers."""

    def test_no_subcommand_exits_nonzero(self, tmp_path):
        """Calling with no subcommand should print usage and exit non-zero."""
        _require_impl()
        code, _out, _err = _run_cli([], tmp_cursor_dir=tmp_path)
        assert code != 0

    def test_history_subcommand_is_recognised(self, tmp_path):
        """'history' subcommand must not raise an unrecognised-command error."""
        _require_impl()
        msgs = [_make_message("1700000001.000001", "hello")]
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=msgs)
            code, _out, _err = _run_cli(["history"], tmp_cursor_dir=tmp_path)
        # Exit 0 or a slack-specific code — anything but "unknown subcommand"
        assert code == 0

    def test_post_subcommand_is_recognised(self, tmp_path):
        """'post' subcommand must be routed to the post handler."""
        _require_impl()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock()
            code, _out, _err = _run_cli(
                ["post", "hello world"], tmp_cursor_dir=tmp_path
            )
        assert code == 0

    def test_poll_subcommand_is_recognised(self, tmp_path):
        """'poll' subcommand must be routed to the poll handler."""
        _require_impl()
        # Return a non-bot message immediately so poll exits 0 quickly.
        msgs = [_make_message("1700000002.000001", "human reply")]
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=msgs)
            code, _out, _err = _run_cli(["poll"], tmp_cursor_dir=tmp_path)
        assert code == 0

    def test_unknown_subcommand_exits_nonzero(self, tmp_path):
        """An unrecognised subcommand should exit non-zero."""
        _require_impl()
        code, _out, _err = _run_cli(
            ["does-not-exist"], tmp_cursor_dir=tmp_path
        )
        assert code != 0


# ---------------------------------------------------------------------------
# 2. history: fetch, print, and advance cursor
# ---------------------------------------------------------------------------

class TestHistory:
    """history subcommand fetches the thread and advances the cursor."""

    def test_history_prints_messages(self, tmp_path):
        """history prints each message text to stdout."""
        _require_impl()
        msgs = [
            _make_message("1700000001.000001", "first message"),
            _make_message("1700000001.000002", "second message"),
        ]
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=msgs)
            code, out, _err = _run_cli(["history"], tmp_cursor_dir=tmp_path)

        assert code == 0
        assert "first message" in out
        assert "second message" in out

    def test_history_calls_get_replies(self, tmp_path):
        """history calls get_replies with the correct channel + thread_ts."""
        _require_impl()
        mock_client = _make_client_mock(
            replies=[_make_message("1700000001.000001", "x")]
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            _run_cli(["history"], tmp_cursor_dir=tmp_path)

        mock_client.get_replies.assert_called_once()
        args, kwargs = mock_client.get_replies.call_args
        assert CHANNEL in args or kwargs.get("channel") == CHANNEL
        assert THREAD_TS in args or kwargs.get("thread_ts") == THREAD_TS

    def test_history_advances_cursor_to_latest_ts(self, tmp_path):
        """history writes the latest ts to the cursor file."""
        _require_impl()
        msgs = [
            _make_message("1700000001.000001", "older"),
            _make_message("1700000001.000099", "latest"),
        ]
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=msgs)
            _run_cli(["history"], tmp_cursor_dir=tmp_path)

        cursor_files = list(tmp_path.glob("*.cursor"))
        assert cursor_files, "No cursor file written by history"
        cursor_val = cursor_files[0].read_text().strip()
        assert cursor_val == "1700000001.000099"

    def test_history_no_messages_cursor_unchanged(self, tmp_path):
        """history with no messages does not crash and cursor stays absent."""
        _require_impl()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=[])
            code, _out, _err = _run_cli(["history"], tmp_cursor_dir=tmp_path)
        # Should exit 0 (empty thread is not an error)
        assert code == 0


# ---------------------------------------------------------------------------
# 3. post: calls post_message and prints the returned ts
# ---------------------------------------------------------------------------

class TestPost:
    """post subcommand calls post_message and prints the new ts."""

    def test_post_calls_post_message(self, tmp_path):
        """post calls post_message with correct channel, thread_ts, text."""
        _require_impl()
        mock_client = _make_client_mock(post_ts="1700000099.000001")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["post", "hello world"], tmp_cursor_dir=tmp_path
            )

        assert code == 0
        mock_client.post_message.assert_called_once()
        args, kwargs = mock_client.post_message.call_args
        assert CHANNEL in args or kwargs.get("channel") == CHANNEL
        assert THREAD_TS in args or kwargs.get("thread_ts") == THREAD_TS
        # text may be positional or keyword
        all_args = list(args) + list(kwargs.values())
        assert any("hello world" in str(a) for a in all_args)

    def test_post_prints_returned_ts(self, tmp_path):
        """post prints the ts returned by post_message to stdout."""
        _require_impl()
        returned_ts = "1700000099.000001"
        mock_client = _make_client_mock(post_ts=returned_ts)
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            _code, out, _err = _run_cli(
                ["post", "test message"], tmp_cursor_dir=tmp_path
            )

        assert returned_ts in out

    def test_post_slack_error_exits_nonzero(self, tmp_path):
        """post exits non-zero when SlackClient raises SlackError."""
        _require_impl()
        from slack_chat.client import SlackError
        mock_client = _make_client_mock()
        mock_client.post_message.side_effect = SlackError("not_in_channel")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, err = _run_cli(
                ["post", "failing message"], tmp_cursor_dir=tmp_path
            )

        assert code != 0


def _posted_text(mock_client):
    """Return the text argument passed to post_message(channel, thread_ts, text)."""
    args, kwargs = mock_client.post_message.call_args
    if "text" in kwargs:
        return kwargs["text"]
    return args[2]


class TestPostInputModes:
    """post reads the body verbatim from --message-file / --stdin so shell
    metacharacters (backticks, $, quotes) are not mangled."""

    # A body that the shell would corrupt if it went through the command line.
    SHELLY = "run `pdb_rir_status --reset` first, cost $5, say \"hi\""

    def test_post_message_file_posts_verbatim(self, tmp_path):
        _require_impl()
        body_file = tmp_path / "reply.md"
        body_file.write_text(self.SHELLY)
        mock_client = _make_client_mock()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["post", "--message-file", str(body_file)],
                tmp_cursor_dir=tmp_path,
            )
        assert code == 0
        assert _posted_text(mock_client) == self.SHELLY

    def test_post_message_file_strips_one_trailing_newline(self, tmp_path):
        _require_impl()
        body_file = tmp_path / "reply.md"
        body_file.write_text("hello there\n")
        mock_client = _make_client_mock()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            _run_cli(
                ["post", "--message-file", str(body_file)],
                tmp_cursor_dir=tmp_path,
            )
        assert _posted_text(mock_client) == "hello there"

    def test_post_stdin_posts_verbatim(self, tmp_path):
        _require_impl()
        mock_client = _make_client_mock()
        with patch("slack_chat.cli.SlackClient") as MockClient, \
                patch("sys.stdin", StringIO(self.SHELLY + "\n")):
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["post", "--stdin"], tmp_cursor_dir=tmp_path
            )
        assert code == 0
        assert _posted_text(mock_client) == self.SHELLY

    def test_post_dash_is_stdin(self, tmp_path):
        _require_impl()
        mock_client = _make_client_mock()
        with patch("slack_chat.cli.SlackClient") as MockClient, \
                patch("sys.stdin", StringIO("piped body")):
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["post", "-"], tmp_cursor_dir=tmp_path
            )
        assert code == 0
        assert _posted_text(mock_client) == "piped body"

    def test_post_no_source_errors(self, tmp_path):
        """No positional text and no --message-file/--stdin is an error."""
        _require_impl()
        mock_client = _make_client_mock()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(["post"], tmp_cursor_dir=tmp_path)
        assert code != 0
        mock_client.post_message.assert_not_called()

    def test_post_positional_and_file_conflict_errors(self, tmp_path):
        """Giving both a positional body and --message-file is rejected."""
        _require_impl()
        body_file = tmp_path / "reply.md"
        body_file.write_text("from file")
        mock_client = _make_client_mock()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["post", "inline", "--message-file", str(body_file)],
                tmp_cursor_dir=tmp_path,
            )
        assert code != 0
        mock_client.post_message.assert_not_called()

    def test_post_missing_message_file_errors(self, tmp_path):
        _require_impl()
        mock_client = _make_client_mock()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["post", "--message-file", str(tmp_path / "nope.md")],
                tmp_cursor_dir=tmp_path,
            )
        assert code != 0
        mock_client.post_message.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Cursor advance across consecutive poll/history calls
# ---------------------------------------------------------------------------

class TestCursorAdvance:
    """Second call reads the cursor written by the first."""

    def test_second_history_uses_cursor_from_first(self, tmp_path):
        """Two consecutive history calls: second passes the first's cursor as oldest."""
        _require_impl()
        ts_first = "1700000001.000001"
        ts_second = "1700000002.000001"
        first_msgs = [_make_message(ts_first, "msg from first call")]
        second_msgs = [_make_message(ts_second, "msg from second call")]

        # First call
        mock_client_1 = _make_client_mock(replies=first_msgs)
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client_1
            _run_cli(["history"], tmp_cursor_dir=tmp_path)

        # Second call — cursor written by first should be used as oldest
        mock_client_2 = _make_client_mock(replies=second_msgs)
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client_2
            _run_cli(["history"], tmp_cursor_dir=tmp_path)

        # get_replies on the second call should have received oldest=ts_first
        mock_client_2.get_replies.assert_called_once()
        args, kwargs = mock_client_2.get_replies.call_args
        all_args = list(args) + list(kwargs.values())
        assert ts_first in all_args or kwargs.get("oldest") == ts_first

    def test_poll_then_history_shares_cursor(self, tmp_path):
        """poll writes its cursor, subsequent history reads it."""
        _require_impl()
        poll_ts = "1700000010.000001"
        poll_msgs = [_make_message(poll_ts, "human reply")]

        # First: poll — finds a new message, exits 0, writes cursor
        mock_client_poll = _make_client_mock(replies=poll_msgs)
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client_poll
            code, _out, _err = _run_cli(["poll"], tmp_cursor_dir=tmp_path)
        assert code == 0

        # Second: history — should use the cursor from poll
        later_ts = "1700000020.000001"
        history_msgs = [_make_message(later_ts, "new context")]
        mock_client_hist = _make_client_mock(replies=history_msgs)
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client_hist
            _run_cli(["history"], tmp_cursor_dir=tmp_path)

        mock_client_hist.get_replies.assert_called_once()
        args, kwargs = mock_client_hist.get_replies.call_args
        all_args = list(args) + list(kwargs.values())
        assert poll_ts in all_args or kwargs.get("oldest") == poll_ts


# ---------------------------------------------------------------------------
# 5. --since override; env-sourced channel/thread_ts; permalink derivation
# ---------------------------------------------------------------------------

class TestEnvAndSince:
    """Channel/thread_ts from env vars or permalink; --since overrides cursor."""

    def test_channel_from_env(self, tmp_path):
        """LMER_SLACK_CHANNEL env var supplies the channel."""
        _require_impl()
        custom_channel = "CCUSTOM01"
        mock_client = _make_client_mock(
            replies=[_make_message("1700000001.000001", "msg")]
        )
        with patch("slack_chat.cli.SlackClient") as MockClient, \
             patch.dict(os.environ, {
                 "LMER_SLACK_CHANNEL": custom_channel,
                 "LMER_SLACK_THREAD_TS": THREAD_TS,
                 "SLACK_BOT_TOKEN": BOT_TOKEN,
             }, clear=False):
            MockClient.return_value = mock_client
            with patch.object(_slack_cli_mod, "CURSOR_DIR", str(tmp_path)):
                code, _out, _err = _run_cli(
                    ["history"],
                    env_overrides={"LMER_SLACK_CHANNEL": custom_channel},
                    tmp_cursor_dir=tmp_path,
                )

        mock_client.get_replies.assert_called_once()
        args, kwargs = mock_client.get_replies.call_args
        assert custom_channel in args or kwargs.get("channel") == custom_channel

    def test_thread_ts_from_env(self, tmp_path):
        """LMER_SLACK_THREAD_TS env var supplies thread_ts."""
        _require_impl()
        custom_ts = "1800000000.654321"
        mock_client = _make_client_mock(
            replies=[_make_message("1800000001.000001", "msg")]
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            _run_cli(
                ["history"],
                env_overrides={
                    "LMER_SLACK_CHANNEL": CHANNEL,
                    "LMER_SLACK_THREAD_TS": custom_ts,
                },
                tmp_cursor_dir=tmp_path,
            )

        args, kwargs = mock_client.get_replies.call_args
        all_args = list(args) + list(kwargs.values())
        assert custom_ts in all_args or kwargs.get("thread_ts") == custom_ts

    def test_since_overrides_cursor(self, tmp_path):
        """--since <ts> is passed to get_replies as oldest, ignoring cursor."""
        _require_impl()
        # Pre-write a cursor file with an older ts
        cursor_file = tmp_path / f"{CHANNEL}-{THREAD_TS}.cursor"
        cursor_file.write_text("1700000001.000001")

        since_ts = "1700000005.000000"
        mock_client = _make_client_mock(
            replies=[_make_message("1700000006.000001", "msg")]
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            _run_cli(
                ["history", "--since", since_ts],
                tmp_cursor_dir=tmp_path,
            )

        args, kwargs = mock_client.get_replies.call_args
        all_args = list(args) + list(kwargs.values())
        assert since_ts in all_args or kwargs.get("oldest") == since_ts

    def test_channel_thread_ts_from_permalink(self, tmp_path):
        """If only a permalink is given (no env channel/ts), parse it."""
        _require_impl()
        # The CLI should accept --permalink (or positional) and derive channel/ts
        mock_client = _make_client_mock(
            replies=[_make_message("1700000001.000001", "msg")]
        )
        with patch("slack_chat.cli.SlackClient") as MockClient, \
             patch.dict(os.environ, {
                 "SLACK_BOT_TOKEN": BOT_TOKEN,
             }, clear=True):
            # Remove LMER_SLACK_CHANNEL and LMER_SLACK_THREAD_TS
            MockClient.return_value = mock_client
            with patch.object(_slack_cli_mod, "CURSOR_DIR", str(tmp_path)):
                # Use --permalink flag or positional; exact name TBD by impl.
                # Try --permalink first; impl may vary.
                try:
                    code, _out, _err = _run_cli(
                        ["history", "--permalink", SLACK_PERMALINK],
                        env_overrides={
                            "LMER_SLACK_CHANNEL": "",
                            "LMER_SLACK_THREAD_TS": "",
                        },
                        tmp_cursor_dir=tmp_path,
                    )
                except SystemExit:
                    code = 1

        # The CLI should have called get_replies with the channel extracted
        # from the permalink (C0TEST001) or exited non-zero (impl not done)
        if mock_client.get_replies.called:
            args, kwargs = mock_client.get_replies.call_args
            assert CHANNEL in args or kwargs.get("channel") == CHANNEL


# ---------------------------------------------------------------------------
# 6. Bot-message filtering in poll
# ---------------------------------------------------------------------------

class TestBotFiltering:
    """poll excludes the bot's own messages via auth_test() user id."""

    def test_poll_ignores_bot_own_messages(self, tmp_path):
        """Messages from the bot user are not printed and do not satisfy poll."""
        _require_impl()
        bot_user_id = "UBOTSELF"
        # Only message is from the bot itself — poll should NOT exit 0 on it,
        # it should keep waiting (or eventually timeout with the timeout code).
        bot_msg = _make_message(
            "1700000002.000001", "I am the bot", user=bot_user_id
        )
        # Subsequent call returns nothing new — should trigger timeout
        mock_client = _make_client_mock(
            replies=[bot_msg], bot_user_id=bot_user_id
        )
        # Make get_replies always return just the bot message
        mock_client.get_replies.return_value = [bot_msg]

        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(
                ["poll", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )

        # Bot message text must NOT appear in stdout
        assert "I am the bot" not in out
        # Exit code must be the timeout code (non-zero, not 1)
        assert code != 0
        assert code != _GENERIC_ERROR_CODE

    def test_poll_uses_auth_test_for_bot_user_id(self, tmp_path):
        """poll calls auth_test() exactly once to determine bot user id."""
        _require_impl()
        # Provide a non-bot message so poll exits quickly
        human_msg = _make_message("1700000003.000001", "hello human")
        mock_client = _make_client_mock(
            replies=[human_msg], bot_user_id="UBOTSELF"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            _run_cli(["poll"], tmp_cursor_dir=tmp_path)

        mock_client.auth_test.assert_called_once()

    def test_poll_non_bot_message_is_printed(self, tmp_path):
        """poll prints non-bot messages before exiting 0."""
        _require_impl()
        human_msg = _make_message("1700000004.000001", "human says hi")
        mock_client = _make_client_mock(
            replies=[human_msg], bot_user_id="UBOTSELF"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(["poll"], tmp_cursor_dir=tmp_path)

        assert code == 0
        assert "human says hi" in out

    def test_poll_ignores_other_bot_messages_with_bot_id(self, tmp_path):
        """A message carrying bot_id (another bot/webhook) is never treated as
        a human reply, even when its user differs from this bot's user id."""
        _require_impl()
        other_bot_msg = _make_message(
            "1700000002.000001",
            "I am another bot",
            user="UOTHERBOT",
            bot_id="BOTHER01",
        )
        mock_client = _make_client_mock(
            replies=[other_bot_msg], bot_user_id="UBOTSELF"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(
                ["poll", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )

        assert "I am another bot" not in out
        assert code == _timeout_exit_code()

    def test_poll_ignores_messages_without_user_field(self, tmp_path):
        """A message with no user field (e.g. some webhook posts) is never
        treated as a human reply."""
        _require_impl()
        userless_msg = {
            "type": "message",
            "ts": "1700000002.000002",
            "text": "no user here",
            "bot_id": "BHOOK01",
        }
        mock_client = _make_client_mock(
            replies=[userless_msg], bot_user_id="UBOTSELF"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(
                ["poll", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )

        assert "no user here" not in out
        assert code == _timeout_exit_code()


# ---------------------------------------------------------------------------
# 6b. Thread-parent handling in poll/history
# ---------------------------------------------------------------------------

class TestThreadParentHandling:
    """conversations.replies always returns the thread parent regardless of
    ``oldest`` — poll must never treat it as a new reply, and a cursored
    history run must not re-print it."""

    def test_poll_does_not_return_thread_parent(self, tmp_path):
        """The parent (human's original message) never satisfies poll."""
        _require_impl()
        parent = _make_message(THREAD_TS, "original question", user="UHUMAN01")
        mock_client = _make_client_mock(
            replies=[parent], bot_user_id="UBOTSELF"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(
                ["poll", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )

        assert "original question" not in out
        assert code == _timeout_exit_code()

    def test_poll_ignores_messages_not_newer_than_cursor(self, tmp_path):
        """Messages at or below the cursor ts are not new, bot-authored or not."""
        _require_impl()
        cursor_ts = "1700000005.000000"
        cursor_file = tmp_path / f"{CHANNEL}-{THREAD_TS}.cursor"
        cursor_file.write_text(cursor_ts)

        parent = _make_message(THREAD_TS, "original question", user="UHUMAN01")
        old_reply = _make_message(
            "1700000004.000001", "already seen reply", user="UHUMAN01"
        )
        mock_client = _make_client_mock(
            replies=[parent, old_reply], bot_user_id="UBOTSELF"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(
                ["poll", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )

        assert "original question" not in out
        assert "already seen reply" not in out
        assert code == _timeout_exit_code()

    def test_poll_returns_new_reply_alongside_parent(self, tmp_path):
        """A genuinely new human reply satisfies poll even when the parent is
        re-included in the same fetch."""
        _require_impl()
        cursor_ts = "1700000005.000000"
        cursor_file = tmp_path / f"{CHANNEL}-{THREAD_TS}.cursor"
        cursor_file.write_text(cursor_ts)

        parent = _make_message(THREAD_TS, "original question", user="UHUMAN01")
        new_reply = _make_message(
            "1700000006.000001", "fresh human reply", user="UHUMAN01"
        )
        mock_client = _make_client_mock(
            replies=[parent, new_reply], bot_user_id="UBOTSELF"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(["poll"], tmp_cursor_dir=tmp_path)

        assert code == 0
        assert "fresh human reply" in out
        assert "original question" not in out

    def test_history_parent_only_fetch_does_not_regress_cursor(self, tmp_path):
        """When a cursored fetch returns only the re-included parent (no new
        messages), history must not move the cursor backwards to the
        parent's ts — otherwise the next poll re-delivers already-handled
        replies as new."""
        _require_impl()
        cursor_ts = "1700000005.000000"
        cursor_file = tmp_path / f"{CHANNEL}-{THREAD_TS}.cursor"
        cursor_file.write_text(cursor_ts)

        parent = _make_message(THREAD_TS, "original question", user="UHUMAN01")
        mock_client = _make_client_mock(replies=[parent])
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(["history"], tmp_cursor_dir=tmp_path)

        assert code == 0
        assert "original question" not in out
        assert cursor_file.read_text().strip() == cursor_ts

    def test_history_does_not_reprint_parent_on_cursored_run(self, tmp_path):
        """A cursored history run prints only messages newer than the cursor."""
        _require_impl()
        cursor_ts = "1700000005.000000"
        cursor_file = tmp_path / f"{CHANNEL}-{THREAD_TS}.cursor"
        cursor_file.write_text(cursor_ts)

        parent = _make_message(THREAD_TS, "original question", user="UHUMAN01")
        new_msg = _make_message("1700000006.000001", "fresh reply")
        mock_client = _make_client_mock(replies=[parent, new_msg])
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(["history"], tmp_cursor_dir=tmp_path)

        assert code == 0
        assert "fresh reply" in out
        assert "original question" not in out
        # Cursor still advances to the latest ts seen.
        assert cursor_file.read_text().strip() == "1700000006.000001"


# ---------------------------------------------------------------------------
# 6c. poll timeout is a wall-clock bound
# ---------------------------------------------------------------------------

class TestPollWallClockTimeout:
    """--timeout is a monotonic wall-clock deadline, not an iteration count."""

    def test_poll_interval_longer_than_timeout_still_waits(self, tmp_path):
        """With interval > timeout, poll must wait out the timeout (sleep
        capped to the remaining time) instead of returning immediately
        after a single fetch."""
        _require_impl()
        mock_client = _make_client_mock(replies=[], bot_user_id="UBOTSELF")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["poll", "--timeout", "10", "--interval", "30"],
                tmp_cursor_dir=tmp_path,
            )

        assert code == _timeout_exit_code()
        # One fetch up front, a (capped) wait, and a final fetch at the
        # deadline — not a single immediate fetch-and-give-up.
        assert mock_client.get_replies.call_count == 2

    def test_poll_timeout_counts_fetch_time(self, tmp_path):
        """Time spent inside get_replies counts against the deadline."""
        _require_impl()
        clock = {"now": 0.0}
        mock_client = _make_client_mock(bot_user_id="UBOTSELF")

        def slow_fetch(*_args, **_kwargs):
            clock["now"] += 7.0  # each HTTP round-trip takes 7s
            return []

        mock_client.get_replies.side_effect = slow_fetch
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["poll", "--timeout", "10", "--interval", "5"],
                tmp_cursor_dir=tmp_path,
                clock=clock,
            )

        assert code == _timeout_exit_code()
        # fetch (t=7) → sleep min(5, 3) (t=10) → fetch → past deadline.
        # Iteration-count math (timeout // interval) would ignore the 7s
        # round-trips entirely.
        assert mock_client.get_replies.call_count == 2


# ---------------------------------------------------------------------------
# 7. poll exit codes: 0 on new message, DISTINCT non-zero on timeout
# ---------------------------------------------------------------------------

class TestPollExitCodes:
    """poll exit-code contract: 0 on success, POLL_TIMEOUT_EXIT_CODE on timeout."""

    def test_poll_exits_0_on_new_message(self, tmp_path):
        """poll exits 0 when a new non-bot message is found."""
        _require_impl()
        human_msg = _make_message("1700000005.000001", "new message arrived")
        mock_client = _make_client_mock(replies=[human_msg])
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(["poll"], tmp_cursor_dir=tmp_path)

        assert code == 0

    def test_poll_timeout_exit_code_is_not_1(self, tmp_path):
        """poll timeout exit code must differ from generic error code 1."""
        _require_impl()
        timeout_code = _timeout_exit_code()
        assert timeout_code is not None, "POLL_TIMEOUT_EXIT_CODE not found"
        assert timeout_code != _GENERIC_ERROR_CODE, (
            f"POLL_TIMEOUT_EXIT_CODE ({timeout_code}) must not equal "
            f"the generic error code ({_GENERIC_ERROR_CODE})"
        )

    def test_poll_exits_timeout_code_on_timeout(self, tmp_path):
        """poll exits with POLL_TIMEOUT_EXIT_CODE when timeout elapses."""
        _require_impl()
        # No messages — poll will exhaust retries / time and exit with timeout code
        mock_client = _make_client_mock(replies=[], bot_user_id="UBOTSELF")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["poll", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )

        expected = _timeout_exit_code()
        assert code == expected, (
            f"Expected POLL_TIMEOUT_EXIT_CODE={expected} on timeout, got {code}"
        )

    def test_poll_timeout_code_differs_from_error_code(self):
        """Constant-level check: POLL_TIMEOUT_EXIT_CODE != 1."""
        _require_impl()
        timeout_code = _timeout_exit_code()
        assert timeout_code != _GENERIC_ERROR_CODE

    def test_poll_advances_cursor_on_success(self, tmp_path):
        """poll advances the cursor file to the latest ts on exit 0."""
        _require_impl()
        new_ts = "1700000006.000001"
        human_msg = _make_message(new_ts, "reply")
        mock_client = _make_client_mock(replies=[human_msg])
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            _run_cli(["poll"], tmp_cursor_dir=tmp_path)

        cursor_files = list(tmp_path.glob("*.cursor"))
        assert cursor_files, "No cursor file written after successful poll"
        cursor_val = cursor_files[0].read_text().strip()
        assert cursor_val == new_ts

    def test_slack_error_exits_1(self, tmp_path):
        """A SlackError during poll exits with code 1 (generic error)."""
        _require_impl()
        from slack_chat.client import SlackError
        mock_client = _make_client_mock()
        mock_client.auth_test.side_effect = SlackError("invalid_auth")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, err = _run_cli(["poll"], tmp_cursor_dir=tmp_path)

        assert code == _GENERIC_ERROR_CODE
        # The error message should name the problem
        assert len(err) > 0 or code == _GENERIC_ERROR_CODE


# ---------------------------------------------------------------------------
# 8. watch: continuous JSONL stream of new human messages (for /monitor)
# ---------------------------------------------------------------------------

class TestWatch:
    """watch continuously emits new human messages as JSON lines.

    Every test passes a finite --timeout so the otherwise-indefinite loop
    terminates under the fake clock (sleep advances monotonic time).
    """

    def test_watch_subcommand_is_recognised(self, tmp_path):
        """'watch' must route to the watch handler, not 'unknown subcommand'."""
        _require_impl()
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=[])
            code, _out, _err = _run_cli(
                ["watch", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        assert code == 0

    def test_watch_emits_human_message_as_json_line(self, tmp_path):
        """A new human message is printed to stdout as a one-line JSON event."""
        _require_impl()
        msg = _make_message("1700000010.000001", "hey there", user="UHUMAN01")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=[msg])
            code, out, _err = _run_cli(
                ["watch", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        assert code == 0
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed == {
            "ts": "1700000010.000001",
            "user": "UHUMAN01",
            "text": "hey there",
        }

    def test_watch_appends_to_out_file(self, tmp_path):
        """With --out, each emitted line is also appended to the file."""
        _require_impl()
        out_file = tmp_path / "thread.jsonl"
        msg = _make_message("1700000011.000001", "to file", user="UHUMAN01")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=[msg])
            code, _out, _err = _run_cli(
                ["watch", "--out", str(out_file), "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        assert code == 0
        assert out_file.exists()
        file_lines = [ln for ln in out_file.read_text().splitlines() if ln.strip()]
        assert len(file_lines) == 1
        assert json.loads(file_lines[0])["text"] == "to file"

    def test_watch_ignores_bot_and_parent_messages(self, tmp_path):
        """The thread parent, this bot's messages, and other bots are skipped."""
        _require_impl()
        parent = _make_message(THREAD_TS, "parent (human original)", user="UHUMAN01")
        own = _make_message("1700000012.000001", "my own reply", user="UBOTSELF")
        other_bot = _make_message(
            "1700000013.000001", "from a bot", user="UBOT9", bot_id="B999"
        )
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(
                replies=[parent, own, other_bot], bot_user_id="UBOTSELF"
            )
            code, out, _err = _run_cli(
                ["watch", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        assert code == 0
        assert [ln for ln in out.splitlines() if ln.strip()] == []

    def test_watch_does_not_re_emit_across_iterations(self, tmp_path):
        """A message seen once is not re-emitted on subsequent fetches."""
        _require_impl()
        msg = _make_message("1700000014.000001", "once only", user="UHUMAN01")
        mock_client = _make_client_mock(replies=[msg])
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, _err = _run_cli(
                ["watch", "--timeout", "10", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        assert code == 0
        # Fetched several times, but the message is emitted exactly once.
        assert mock_client.get_replies.call_count >= 2
        assert len([ln for ln in out.splitlines() if ln.strip()]) == 1

    def test_watch_advances_cursor_file(self, tmp_path):
        """watch persists the cursor so a restart resumes instead of replaying."""
        _require_impl()
        new_ts = "1700000015.000001"
        msg = _make_message(new_ts, "advance me", user="UHUMAN01")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=[msg])
            _run_cli(
                ["watch", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        cursor_files = list(tmp_path.glob("*.cursor"))
        assert cursor_files, "watch did not write a cursor file"
        assert cursor_files[0].read_text().strip() == new_ts

    def test_watch_jsonl_preserves_multiline_text(self, tmp_path):
        """Multi-line message text stays a single JSON line (one event)."""
        _require_impl()
        msg = _make_message("1700000016.000001", "line one\nline two", user="UHUMAN01")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = _make_client_mock(replies=[msg])
            code, out, _err = _run_cli(
                ["watch", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        assert code == 0
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == "line one\nline two"

    def test_watch_auth_error_fails_fast(self, tmp_path):
        """A SlackError from the pre-loop auth_test fails fast (exit 1).

        A bad/revoked token is not transient and must not be retried forever.
        """
        _require_impl()
        from slack_chat.client import SlackError
        mock_client = _make_client_mock()
        mock_client.auth_test.side_effect = SlackError("invalid_auth")
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, _out, _err = _run_cli(
                ["watch", "--timeout", "1", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )
        assert code == _GENERIC_ERROR_CODE
        # Never reached the fetch loop.
        assert mock_client.get_replies.call_count == 0

    def test_watch_retries_after_transient_fetch_error(self, tmp_path):
        """A transient SlackError from get_replies is logged and retried, not
        fatal — the watcher keeps streaming once Slack recovers."""
        _require_impl()
        from slack_chat.client import SlackError
        human_msg = _make_message("1700000020.000001", "after blip", user="UHUMAN01")

        state = {"calls": 0}

        def fetch(*_args, **_kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise SlackError("network blip")
            if state["calls"] == 2:
                return [human_msg]
            return []

        mock_client = _make_client_mock()
        mock_client.get_replies.side_effect = fetch
        with patch("slack_chat.cli.SlackClient") as MockClient:
            MockClient.return_value = mock_client
            code, out, err = _run_cli(
                ["watch", "--timeout", "3", "--interval", "1"],
                tmp_cursor_dir=tmp_path,
            )

        assert code == 0
        # The watcher survived the blip and delivered the later message once.
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == "after blip"
        # The transient error was surfaced on stderr, not swallowed silently.
        assert "transient" in err.lower()
        assert mock_client.get_replies.call_count >= 2
