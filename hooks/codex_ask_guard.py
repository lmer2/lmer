#!/usr/bin/env python3
"""Codex Stop hook: resume a turn when an operator answers ``lmer-ask``.

Codex can finish a turn while the shell process behind ``lmer-ask ask`` is
still polling.  The eventual answer is then printed into a terminal nobody is
watching, after the model has stopped, and does not itself create another turn.

This hook keeps Stop synchronous while the ask channel has an answerable
question.  As soon as any question has an answer sidecar without a read
receipt, Codex's native ``decision: block`` response creates a continuation
prompt whose sole job is to make the agent read the oldest such answer through
the existing file channel.  The answer itself is never read or copied here.

Every uncertainty fails open.  An absent/malformed channel, a closed question,
the internal timeout, a non-interactive fan-out child, or a continuation that
already has ``stop_hook_active`` all let Codex stop normally.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable


# Ask-channel on-disk shape, inlined from src/ask_channel/protocol.py — the
# source of truth for these names. Hooks are standalone-stdlib and import no
# project code, so a change to the suffixes there must be mirrored here.
ASK_DIR_ENV = "LMER_ASK_DIR"
QUESTION_SUFFIX = ".question.json"
ANSWER_SUFFIX = ".answer.json"
CLOSED_SUFFIX = ".closed.json"
READ_SUFFIX = ".read.json"
WAIT_TIMEOUT_SECONDS = 3540.0
WAIT_INTERVAL_SECONDS = 0.25


def env_flag(raw: str | None, *, default: bool = False) -> bool:
    """Replicate :func:`lmer_cli.util.get_bool_env` without project imports."""
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return default


def unread_answer_ids(names: list[str]) -> list[int]:
    """Answered question ids without read receipts, oldest first."""
    present = set(names)
    unread = []
    for name in names:
        if not name.endswith(QUESTION_SUFFIX):
            continue
        stem = name[: -len(QUESTION_SUFFIX)]
        if stem + ANSWER_SUFFIX not in present or stem + READ_SUFFIX in present:
            continue
        try:
            unread.append(int(stem))
        except (TypeError, ValueError):
            continue
    return sorted(unread)


def has_answerable_question(names: list[str]) -> bool:
    """Whether any question has neither an answer nor a close sidecar."""
    present = set(names)
    for name in names:
        if not name.endswith(QUESTION_SUFFIX):
            continue
        stem = name[: -len(QUESTION_SUFFIX)]
        try:
            int(stem)
        except (TypeError, ValueError):
            continue
        if (
            stem + ANSWER_SUFFIX not in present
            and stem + CLOSED_SUFFIX not in present
        ):
            return True
    return False


def wait_for_unread_answer(
    ask_dir: str,
    *,
    timeout: float = WAIT_TIMEOUT_SECONDS,
    interval: float = WAIT_INTERVAL_SECONDS,
    listdir: Callable[[str], list[str]] = os.listdir,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Return the oldest unread answer id, waiting while questions are open.

    An answer already present when Stop fires is returned immediately. While
    waiting, every open question is watched so answering an older one cannot
    strand the hook behind a newer question. ``None`` covers timeout, a channel
    with no unread/open question, and every filesystem failure.
    """
    deadline = monotonic() + timeout
    while True:
        try:
            names = listdir(ask_dir)
        except OSError:
            return None
        unread = unread_answer_ids(names)
        if unread:
            return unread[0]
        if not has_answerable_question(names):
            return None
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(interval, remaining))


def build_reason(question_id: int) -> str:
    """Continuation prompt after an answer sidecar becomes visible."""
    stem = f"{question_id:06d}"
    return (
        f"The operator answered lmer-ask question {stem}. Run "
        f"`lmer-ask wait {stem}` now to read the answer, then continue the "
        "task from that answer. Do not ask the question again."
    )


def main(argv: list[str] | None = None) -> int:
    """Read a Codex Stop payload and emit a native continuation if warranted."""
    del argv
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError, TypeError):
        return 0
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return 0
    if env_flag(os.environ.get("LMER_NONINTERACTIVE"), default=False):
        return 0

    ask_dir = os.environ.get(ASK_DIR_ENV, "").strip()
    if not ask_dir or not os.path.isdir(ask_dir):
        return 0
    question_id = wait_for_unread_answer(ask_dir)
    if question_id is None:
        return 0
    json.dump({"decision": "block", "reason": build_reason(question_id)}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
