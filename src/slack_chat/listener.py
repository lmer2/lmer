"""Host-side Slack listener that spawns one ``lmer chat`` session per thread.

``lmer-slack-listener`` is a long-lived process that runs **on the host**,
not inside a container. It holds a Slack socket-mode connection and, when
someone mentions the bot or DMs it, spawns an ``lmer chat <thread-permalink>``
process (see :mod:`slack_chat.sessions`). That spawned agent joins the Slack
thread and does all conversation I/O itself via the in-container ``lmer-slack``
CLI. The listener never relays conversation content - it only spawns and
tracks sessions.

Why the host: lmer launches a container per session, so the thing that
*decides to launch* those containers has to sit one level up, as a sibling to
them. A containerized listener would mean a container launching sibling
containers, which is the messy arrangement this command exists to avoid. The
listener must therefore run on a host with the ``lmer`` CLI and a container
runtime available.

This is the generic counterpart to any product-specific bot: it carries no
``!command`` handling - every non-bot mention or DM simply connects an lmer
chat session to the thread.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from lmer_cli.presets import Preset, load_presets, parse_preset_token
from lmer_cli.tls import ensure_ca_bundle

from .registry import is_thread_connected
from .sessions import SessionManager

logger = logging.getLogger("lmer_slack.listener")

# The Slack app, built lazily by build_app() so importing this module (e.g.
# in tests) does not require a bot token. Handlers reference it for client
# calls; tests replace it with a stub.
app = None

# lmer chat sessions, one per conversational Slack thread. Reconstructed in
# main() after .env is loaded so LMER_SLACK_CHAT_* config is honored.
session_manager = SessionManager()

# Serializes connect attempts: two near-simultaneous events for the same
# thread must not both pass the touch/capacity checks and race into spawn().
_connect_lock = asyncio.Lock()

# Upper bound on the per-thread permalink fetch. It runs inside _connect_lock,
# so without a timeout a slow or hung chat_getPermalink (Slack latency /
# internal retry) would serialize *all* new-thread connects for its full
# duration. A timed-out fetch is treated as a fetch failure (no permalink).
PERMALINK_FETCH_TIMEOUT_SECONDS = 10.0

# The bot's own Slack user ID, resolved once at startup (used to render the
# reconnect mention without hardcoding the bot's name).
_bot_user_id: str | None = None


def _ensure_ca_bundle() -> None:
    """Point OpenSSL's default verify path at certifi's CA bundle.

    Kept as a module-level name because the socket-mode startup path and its tests
    both reference it, but the implementation is shared: the same missing-trust-
    store problem hit the pinned-Node download in :mod:`lmer_platform.ui_build`,
    and one fix in two places is one fix that can drift. See
    :func:`lmer_cli.tls.ensure_ca_bundle` for why it works and why an existing
    ``SSL_CERT_FILE`` is deliberately left alone.
    """
    ensure_ca_bundle()


def _load_env_files() -> None:
    """Populate ``os.environ`` from ``.env`` files the way the main lmer CLI does.

    Sources, in precedence order (highest first):

    1. the **active environment** — an already-exported variable always wins;
    2. the **current working directory's** ``.env`` (the deployment dir);
    3. ``~/.lmer/.env`` — lmer's shared state-dir config.

    ``override=False`` keeps active env vars from being clobbered, and loading
    the cwd file before the state-dir file makes cwd win between the two (the
    first file to set a key keeps it). The state dir is resolved via
    ``lmer_cli.runtime`` so it stays in lockstep with the main CLI.
    """
    from lmer_cli.runtime import lmer_state_dir

    for env_file in (Path.cwd() / ".env", lmer_state_dir() / ".env"):
        if env_file.is_file():
            load_dotenv(env_file, override=False)
            logger.debug("loaded_env_file path=%s", env_file)


def _csv_env_set(name: str, default: str = "") -> set[str]:
    """Parse a comma-separated env var into a set of trimmed, non-empty values."""
    raw = os.getenv(name, default)
    return {s.strip() for s in raw.split(",") if s.strip()}


# Slack user IDs allowed to hold conversational DM sessions. When empty (the
# default) DMs are open to everyone; when set, a DM from anyone not on the
# list is silently ignored. Repopulated from env in main() after .env load.
DM_ALLOWED_USERS: set[str] = _csv_env_set("LMER_SLACK_DM_ALLOWED_USERS")

# Operator-defined startup presets a user can select with the ``$preset:<name>``
# token (see :mod:`lmer_cli.presets`). Empty when LMER_PRESETS_FILE is
# unset. Repopulated from env in main() after .env load, mirroring how the
# session manager and DM allowlist are reconstructed there.
PRESETS: dict[str, Preset] = load_presets()


def build_app(token: str | None = None):
    """Create the Slack ``AsyncApp`` and register the event handlers.

    Deferred (not done at import) so the module can be imported without a
    Slack token. Stores the app on the module global ``app`` and returns it.
    """
    global app
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=token or os.environ.get("SLACK_BOT_TOKEN"))
    app.event("app_mention")(handle_mention)
    app.event("message")(handle_message_event)
    return app


async def _get_bot_user_id() -> str | None:
    """Return the bot's own Slack user ID, caching the auth_test result."""
    global _bot_user_id
    if _bot_user_id is None:
        try:
            auth = await app.client.auth_test()
            _bot_user_id = auth.get("user_id")
        except Exception as e:
            logger.warning("auth_test_failed error=%s", e)
    return _bot_user_id


async def _fetch_thread_permalink(channel: str, thread_ts: str) -> str | None:
    """Fetch the Slack permalink for a thread's parent message.

    The permalink is what ``lmer chat`` accepts as a Slack-thread target.

    Args:
        channel: Channel ID where the thread exists
        thread_ts: Thread timestamp identifier

    Returns:
        Permalink URL, or None if it could not be fetched
    """
    try:
        result = await asyncio.wait_for(
            app.client.chat_getPermalink(
                channel=channel, message_ts=thread_ts
            ),
            timeout=PERMALINK_FETCH_TIMEOUT_SECONDS,
        )
        if result.get("ok"):
            return result.get("permalink")
    except asyncio.TimeoutError:
        logger.error(
            "permalink_fetch_timeout channel=%s thread_ts=%s timeout=%s",
            channel,
            thread_ts,
            PERMALINK_FETCH_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.error(
            "permalink_fetch_failed channel=%s thread_ts=%s error=%s",
            channel,
            thread_ts,
            e,
        )
    return None


async def _post_disconnect_notice(session, reason_text: str) -> None:
    """Post a reconnect hint in a thread whose session went away."""
    bot_user_id = await _get_bot_user_id()
    bot_ref = f"<@{bot_user_id}>" if bot_user_id else "the bot"
    try:
        await app.client.chat_postMessage(
            channel=session.channel,
            thread_ts=session.thread_ts,
            text=f"{reason_text} Mention {bot_ref} to reconnect it to this thread.",
        )
    except Exception as e:
        logger.error(
            "disconnect_notice_failed channel=%s thread_ts=%s error=%s",
            session.channel,
            session.thread_ts,
            e,
        )


async def _post_idle_disconnect_notice(session) -> None:
    """Post the reconnect hint in a thread whose session idled out."""
    await _post_disconnect_notice(
        session,
        (
            f"This conversation was disconnected after "
            f"{session_manager.idle_timeout_minutes} minutes of inactivity."
        ),
    )


async def _post_crash_disconnect_notice(session) -> None:
    """Post the reconnect hint in a thread whose session crashed."""
    await _post_disconnect_notice(
        session, "The session connected to this thread ended unexpectedly."
    )


async def _connect_lmer_session(
    channel: str,
    thread_ts: str,
    say,
    is_dm: bool = False,
    dedup_channel: bool = False,
    preset_name: str | None = None,
) -> None:
    """Attach an lmer chat session to a Slack thread.

    If a session is already running for the thread, this only resets its
    idle timer - the running agent's own ``lmer-slack poll`` picks the new
    message up, so the listener must not double-spawn.

    The whole check/spawn sequence runs under a lock so two
    near-simultaneous events for the same thread cannot race past the
    touch/capacity checks and trigger a spurious spawn failure.

    Args:
        channel: Slack channel ID
        thread_ts: Thread timestamp the session is (or will be) attached to
        say: Function to send a message to the channel
        is_dm: Whether this is a direct-message conversation (adjusts the
            ack so users know to continue inside the thread)
        dedup_channel: When True (top-level DM messages), point the user at
            any session already live in this channel instead of spawning a
            second one. Checked *inside* the connect lock so two
            near-simultaneous top-level DMs - which key on their own ``ts``
            and so slip past the per-thread ``touch()`` dedup below - cannot
            both fall through and spawn two containers in one DM.
        preset_name: Name selected via ``$preset:<name>`` in the triggering
            message, or None. Resolved against the configured presets only on
            the spawn path: an unknown name is rejected with a thread reply
            (no spawn); on an already-tracked thread the token is moot and
            ignored, since the running agent handles the new message itself.
    """
    preset: Preset | None = None
    async with _connect_lock:
        if dedup_channel:
            active = session_manager.get_active_in_channel(channel)
            # Only redirect when the live session is in a *different* thread. A
            # top-level DM @-mention is delivered as both an app_mention (no
            # dedup_channel) and a message.im (dedup_channel=True) for the same
            # ts; if the mention path wins the lock and spawns first, the live
            # session's thread_ts equals this thread_ts, so pointing the user at
            # "this thread" would be redundant. Fall through to the touch() path
            # below instead - it dedups cleanly and resets the idle timer.
            if active and active.thread_ts != thread_ts:
                await say(
                    text=(
                        f"I already have a session connected in this conversation - "
                        f"please continue in <{active.permalink}|this thread>."
                    ),
                    thread_ts=thread_ts,
                )
                return

        if session_manager.touch(channel, thread_ts):
            logger.info(
                "lmer_session_already_active channel=%s thread_ts=%s",
                channel,
                thread_ts,
            )
            return

        # A session this listener did not spawn — e.g. one started manually with
        # `lmer chat <permalink>` from a shell — is invisible to the in-memory
        # manager above, but it registers itself in the host-side session
        # registry. If a live one is attached to this thread, stay out: a second
        # lmer would put two agents in one thread (issue #74). Silent by design
        # — the already-connected session is handling the conversation, so an
        # extra notice would just be noise. A stale entry (dead PID) is treated
        # as absent by is_thread_connected, so a crashed manual session doesn't
        # block reconnection.
        if is_thread_connected(channel, thread_ts):
            logger.info(
                "lmer_session_external_active channel=%s thread_ts=%s",
                channel,
                thread_ts,
            )
            return

        # Resolve a selected preset before spawning. An unknown name is a user
        # error worth flagging clearly (and more actionable than a later "busy"
        # message), so this is checked ahead of the capacity gate. On an
        # already-served thread (handled by the guards above) the token is moot,
        # so this only runs when we are about to spawn a new session.
        if preset_name is not None:
            preset = PRESETS.get(preset_name)
            if preset is None:
                available = ", ".join(sorted(PRESETS)) or "(none configured)"
                logger.warning(
                    "unknown_preset channel=%s thread_ts=%s preset=%s",
                    channel,
                    thread_ts,
                    preset_name,
                )
                await say(
                    text=(
                        f"❌ Unknown preset `{preset_name}`. "
                        f"Available presets: {available}."
                    ),
                    thread_ts=thread_ts,
                )
                return

        if session_manager.at_capacity():
            logger.warning(
                "lmer_session_capacity_reached channel=%s thread_ts=%s max_sessions=%s",
                channel,
                thread_ts,
                session_manager.max_sessions,
            )
            await say(
                text=(
                    "I can't take on another conversation right now "
                    f"(all {session_manager.max_sessions} session slots are busy). "
                    "Please try again in a little while."
                ),
                thread_ts=thread_ts,
            )
            return

        permalink = await _fetch_thread_permalink(channel, thread_ts)
        if not permalink:
            await say(
                text="❌ Could not resolve a permalink for this thread. Please check the logs.",
                thread_ts=thread_ts,
            )
            return

        try:
            await session_manager.spawn(channel, thread_ts, permalink, preset=preset)
        except Exception as e:
            logger.exception(
                "lmer_session_spawn_failed channel=%s thread_ts=%s error=%s",
                channel,
                thread_ts,
                e,
            )
            await say(
                text="❌ Could not start a session for this thread. Please check the logs.",
                thread_ts=thread_ts,
            )
            return

    ack = "Connecting a session to this thread"
    if preset is not None:
        ack += f" using preset `{preset.name}`"
    ack += "... ⏳ (the first reply can take a minute)"
    if is_dm:
        ack += "\nPlease reply *in this thread* to continue the conversation."
    await say(text=ack, thread_ts=thread_ts)


def _dm_user_allowed(user_id: str | None) -> bool:
    """Whether *user_id* may hold a conversational DM session.

    Returns True when the allowlist (``LMER_SLACK_DM_ALLOWED_USERS``) is empty -
    i.e. unset, so DMs are open to everyone - or when the user is on it. A
    missing ``user_id`` is never allowed once a list is configured.
    """
    if not DM_ALLOWED_USERS:
        return True
    return user_id in DM_ALLOWED_USERS


async def handle_mention(event, say):
    """Handle @bot mentions by connecting an lmer chat session.

    Outside a thread, the mention message itself becomes the thread parent
    and a session is attached to it (the bot "starts the thread"). Inside a
    thread, a new session is attached only if none is running - a live
    session sees the mention through its own ``lmer-slack poll``.

    Args:
        event: The app_mention event data from Slack
        say: Function to send a message to the channel
    """
    # Bot-authored messages can quote a literal @bot mention (e.g. the
    # disconnect notice this listener posts) - never let those spawn sessions.
    if event.get("bot_id"):
        return

    user_id = event.get("user")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    # DM detection here uses the channel-ID prefix rather than the
    # ``channel_type == "im"`` test used elsewhere: app_mention payloads don't
    # reliably carry ``channel_type``, whereas Slack DM channel IDs always
    # start with "D". For the allowlist gate it also fails safe (an unknown
    # channel won't be treated as an open DM).
    is_dm = bool(channel and channel.startswith("D"))

    # A mention inside a DM is a conversational DM, so it is subject to the DM
    # allowlist. Channel mentions are never gated - the allowlist is DM-scoped.
    if is_dm and not _dm_user_allowed(user_id):
        logger.info("dm_mention_not_allowlisted user_id=%s channel=%s", user_id, channel)
        return

    logger.info(
        "received_bot_mention user_id=%s channel=%s thread_ts=%s",
        user_id,
        channel,
        thread_ts,
    )

    # Pass is_dm so a session started by mentioning the bot inside a DM gets the
    # same "reply in this thread" hint as the plain-message DM path.
    preset_name = parse_preset_token(event.get("text"))
    await _connect_lmer_session(
        channel, thread_ts, say, is_dm=is_dm, preset_name=preset_name
    )


async def handle_message_event(event, say):
    """Track thread activity and route DM conversations to lmer sessions.

    Two responsibilities:

    1. Any message in a thread with a running session (human replies and
       the session agent's own posts alike) resets that session's idle
       timer. The spawned agent is expected to post progress notes during
       long autonomous work, so a working session keeps itself alive.
    2. In DMs, Slack sends regular 'message' events instead of
       'app_mention'. Non-bot DM messages connect an lmer session to the
       DM thread, mirroring the channel-mention behavior.

    Args:
        event: The message event data from Slack
        say: Function to send a message to the channel
    """
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    # Activity tracking: every message in a tracked thread resets the idle
    # timer, including bot posts from the session's own agent.
    if channel and thread_ts:
        session_manager.touch(channel, thread_ts)

    # The rest is DM session routing only
    if event.get("channel_type") != "im":
        return

    # Skip bot messages and message subtypes (edits, deletes, etc.)
    if event.get("bot_id") or event.get("subtype"):
        return

    user_id = event.get("user")

    # DM allowlist: when LMER_SLACK_DM_ALLOWED_USERS is set, only listed users
    # may hold conversational DM sessions. Anyone else is silently ignored (no
    # session, no reply). Unset = open to everyone.
    if not _dm_user_allowed(user_id):
        logger.info("dm_not_allowlisted user_id=%s channel=%s", user_id, channel)
        return

    logger.info("received_dm_message user_id=%s thread_ts=%s", user_id, thread_ts)

    # DM conversations are effectively serial and most people don't thread in
    # DMs: a top-level message while a session is already live in this DM would
    # spawn a whole new container per message. A top-level message keys on its
    # own ts, so dedup against the active session must happen channel-wide and
    # inside the connect lock (dedup_channel) to stay race-free; a threaded
    # reply keys on its real thread_ts and goes through normal touch/spawn.
    dedup_channel = not event.get("thread_ts")
    preset_name = parse_preset_token(event.get("text"))
    await _connect_lmer_session(
        channel,
        thread_ts,
        say,
        is_dm=True,
        dedup_channel=dedup_channel,
        preset_name=preset_name,
    )


async def _run() -> None:
    """Build the app, start the reaper, and run socket mode until stopped."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        logger.error("slack_app_token_missing")
        raise ValueError(
            "SLACK_APP_TOKEN is required (socket mode app-level token, xapp-...)"
        )

    build_app()

    # Resolve the bot's user ID up front (used in disconnect notices)
    await _get_bot_user_id()

    # Reap exited/idle lmer chat sessions in the background
    reaper_task = asyncio.create_task(
        session_manager.run_reaper(
            on_idle_disconnect=_post_idle_disconnect_notice,
            on_crash=_post_crash_disconnect_notice,
        )
    )

    logger.info(
        "initializing_socket_mode_handler max_sessions=%s idle_timeout_minutes=%s",
        session_manager.max_sessions,
        session_manager.idle_timeout_minutes,
    )
    handler = AsyncSocketModeHandler(app, app_token)
    try:
        await handler.start_async()
    finally:
        reaper_task.cancel()
        await session_manager.shutdown_all()


def main(argv=None) -> int:
    """Console-script entry point for ``lmer-slack-listener``."""
    parser = argparse.ArgumentParser(
        prog="lmer-slack-listener",
        description=(
            "Host-side Slack listener that spawns an 'lmer chat' session per "
            "thread when the bot is mentioned or DMed. Requires SLACK_BOT_TOKEN "
            "and SLACK_APP_TOKEN (socket mode), plus the lmer CLI on PATH."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LMER_SLACK_LOG_LEVEL", "INFO"),
        help="Python logging level (default: INFO; or LMER_SLACK_LOG_LEVEL).",
    )
    parser.add_argument(
        "--lmer-env-file",
        default=None,
        help=(
            "Path to a .env file forwarded to each spawned 'lmer chat' as "
            "'lmer --env-file', so its variables (git tokens, LMER_* settings, "
            "...) reach the chat container even though the spawn cwd has no "
            ".env. Defaults to LMER_SLACK_CHAT_ENV_FILE; when neither is set, "
            "spawning is unchanged."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Load .env (cwd + ~/.lmer/), matching the main lmer CLI, before reading
    # any LMER_SLACK_* settings. Active env vars take precedence.
    _load_env_files()

    # Ensure a usable CA bundle before any TLS handshake (socket-mode WSS /
    # Slack web client). Runs after _load_env_files so a .env-provided
    # SSL_CERT_FILE override is honored.
    _ensure_ca_bundle()

    global session_manager, DM_ALLOWED_USERS, PRESETS
    session_manager = SessionManager(lmer_env_file=args.lmer_env_file)
    DM_ALLOWED_USERS = _csv_env_set("LMER_SLACK_DM_ALLOWED_USERS")
    PRESETS = load_presets()

    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.error("slack_bot_token_missing")
        print(
            "SLACK_BOT_TOKEN is required (Slack bot user OAuth token, xoxb-...).",
            file=sys.stderr,
        )
        return 1

    if not os.environ.get("SLACK_APP_TOKEN"):
        logger.error("slack_app_token_missing")
        print(
            "SLACK_APP_TOKEN is required (socket mode app-level token, xapp-...).",
            file=sys.stderr,
        )
        return 1

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
