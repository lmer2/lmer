#!/usr/bin/env python3
"""
Stop hook: orchestrator signal reminder.

An orchestrated session (``LMER_ASK_DIR`` is set) reports its milestones to
the supervising assistant with ``lmer-signal``. Field reports on the !221
cycle show the miss that matters: a run posts a review round, pushes an MR, or
finishes its fixes — and then ends the turn without signalling. Nothing is
broken and nothing is loud, so the orchestrator learns the state only from the
next stalled-run digest, up to an hour later. The session sat idle on
completed work that whole time, and the operator waited with it.

Like the Slack reply guard, this is the layer where the knowledge is still
fresh: at the moment the agent yields, the turn's evidence is on disk (the
transcript, the run state, the channel dir) and can be checked mechanically.
Prompt rules already say to signal on a milestone, and prompt rules alone were
not enough — the same class of miss the other two Stop hooks exist for.

**It reminds; it never signals.** Auto-signalling would be trivial here and is
deliberately refused: a signal is charged against the orchestrator's context
window and is the one channel that means "a milestone happened". A hook that
emitted one defensively — on a turn that merely looked milestone-shaped —
would teach the orchestrator to distrust the channel, and a signal that means
"some session stopped" means nothing at all. So the hook blocks the stop once
and asks the agent, which knows whether the work is actually reportable. The
reminder text says so explicitly: if the turn was mid-task, stop again.

Trigger (hybrid, per the issue #289 spec): the turn shows **milestone
evidence** — a successful milestone-shaped command in the transcript, or the
run record reporting itself complete — and **no signal-equivalent act**. Three
things count as signal-equivalent and suppress the nudge: a successful
``lmer-signal`` after the last milestone in event order (which also holds back
the facts side, since a completed-run fact persists for the whole session while
a signal is a single event); a signal file in the channel dir newer than this
session's recorded baseline, but *only* when the transcript shows no signal at
all, because otherwise that file is the transcript's own signal counted twice
and unordered evidence would overrule ordered evidence; and a newly opened
``lmer-ask`` question, because an asking turn already notifies the orchestrator
through the ask channel.

The nudge fires once per distinct milestone and at most three times per
session (a marker file in ``/tmp`` keyed on ``LMER_SESSION_ID``), honours
``stop_hook_active`` so it cannot loop within a yield, and fails open
everywhere: unreadable payload, transcript, channel dir, marker, or a failing
``work`` call all let the stop proceed. A guard that trapped a session would
be worse than the late digest it prevents.

**What this hook cannot see, so silence from it means "nothing to report" and
never "nothing happened".** Milestone evidence comes from Bash ``tool_use``
blocks and from ``work resume --json``. A milestone reached any other way is
invisible: a slash command, a subagent's own tool calls, an MCP tool, or a push
made outside the gate wrappers. Review-post wrappers deliberately belong to
that list now: each wrapper reports its own successful post with
``lmer-signal``, so predicting from shell text whether it ran successfully is a
second, weaker owner of the same milestone (and one that misread brace-heavy
commands). The pattern list below is a list of *direct CLI* spellings, not a
classifier. Every gap ends in silence rather than a false nudge, which is the
direction chosen throughout, but it means this hook is a backstop for the
common shapes and not a proof that a turn reported everything it should.

Claude-only, because only the claude harness fires lifecycle hooks; codex and
pi runs keep the prose instruction until the daemon-side watch (issue #294)
lands. Fan-out children (``LMER_NONINTERACTIVE``) are skipped outright — see
the gate in :func:`main`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

# Ask-channel on-disk shape, inlined from src/ask_channel/protocol.py — the
# source of truth for these names. Hooks are standalone-stdlib and import no
# project code, so a change to the suffixes there must be mirrored here.
ASK_DIR_ENV = "LMER_ASK_DIR"
SIGNAL_SUFFIX = ".signal.json"
QUESTION_SUFFIX = ".question.json"
ANSWER_SUFFIX = ".answer.json"
CLOSED_SUFFIX = ".closed.json"

# Session-scoped marker: which milestones this session was already nudged
# about, how many nudges it has spent, and the signal id that was newest the
# last time the guard looked. Keyed on LMER_SESSION_ID, same /tmp template
# style as hooks/run_state_guard.py.
MARKER_TEMPLATE = "/tmp/lmer_signal_guard.{session}.json"

# Hard ceiling on nudges per session. A session that keeps producing
# milestones without signalling has heard the message; three is a reminder,
# more is nagging.
NUDGE_CAP = 3

# A Stop hook runs on every yield, so the one subprocess it makes is bounded
# and a timeout fails open.
WORK_TIMEOUT_SECONDS = 5

# get_bool_env semantics (src/lmer_cli/util.py), replicated inline for the
# same no-project-imports reason as the suffixes above.
_TRUTHY = {"1", "yes", "true"}
_FALSY = {"0", "no", "false"}

# Leading boundary class (start-of-string, shell separator, or path slash) —
# the _SLACK_POST_RE trick from hooks/slack_reply_guard.py. The trailing
# boundary keeps `gate-push-docs` from matching `gate-push`.
#
# What the boundary alone does NOT do, measured: whitespace inside quotes is a
# boundary like any other, so `git commit -m "wire gate-push into CI"` matched.
# _strip_quoted below removes quoted spans for exactly that reason. What
# remains after it is an *unquoted* mention — a heredoc body, or prose in an
# unquoted argument — which still reads as an invocation. Three such stops
# spend the whole NUDGE_CAP, so the cap is the bound on the damage rather than
# the pattern being exact.
_BOUNDARY = r"(?:^|[\s;&|()/])"

# Trailing boundary. Whitespace and end-of-string are the common cases, but a
# command list ends a word too: `gate-push; work log "pushed"` and
# `gate-push && lmer-signal "…"` are real invocations, and requiring whitespace
# there missed them. `-` stays out of the class so `gate-push-docs` is still not
# `gate-push`; `=` is in the flag variant so `--review-file=x` matches.
_TRAIL = r"(?:[\s;&|)]|$)"
_FLAG_TRAIL = r"(?:[\s;&|)=]|$)"

# Quoted spans, blanked out before any pattern search: a command that merely
# *talks about* a milestone (`git commit -m "… gate-push …"`,
# `echo "then run lmer-signal"`) must not read as one. Blanked rather than
# deleted so the surrounding boundary characters survive.
#
# A command with unbalanced quotes can have a real invocation blanked out
# instead (the span runs to the next quote, wherever that is). That loses a
# reminder rather than inventing one, which is the direction this guard
# prefers everywhere else too.
_QUOTED_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'")

# Backslash line continuations, joined before matching. The agent writes long
# review invocations across lines, and the flag that identifies the milestone is
# usually on a later one — the patterns are single-line (`[^\n]*`), so without
# this join a wrapped `gitlab-review … \\\n --review-file …` was invisible.
_CONTINUATION_RE = re.compile(r"\\\n\s*")

# EXTEND ME: every command that completes reportable work belongs here. This
# tuple is the single place the guard learns what a milestone looks like, so
# adding a milestone-shaped verb to lmer (a new review post, a new push path,
# a new completion command) means adding a row here in the same MR — a
# reviewer can check that list against the diff. A command missing from it is
# not an error anywhere; it just silently never reminds anyone, which is the
# failure mode this comment exists to convert into a review-time question.
#
# Spellings here are pinned against the real CLIs by
# tests/test_signal_guard.py::TestPatternsMatchRealCommands, which reads the
# argparse parsers and bin/ directory: the first version of this list
# invented a `--post-review` flag that exists on neither reviewer CLI, and only
# the fixtures agreed with it. A flag renamed upstream now fails CI here.
#
# Each row is (evidence label, pattern). The label goes into the block reason
# so the agent is told which act it is being asked about.
_MILESTONE_PATTERNS = (
    ("gate-push", re.compile(_BOUNDARY + r"gate-push" + _TRAIL)),
    ("gitlab-review --create-mr",
     re.compile(_BOUNDARY + r"gitlab-review\s[^\n]*--create-mr" + _FLAG_TRAIL)),
    # Posting a review is `--review-file` on both reviewer CLIs; there is no
    # --post-review flag. Wrapper scripts are absent on purpose: they signal
    # their own successful post and command-text inference cannot improve on it.
    ("gitlab-review --review-file",
     re.compile(_BOUNDARY + r"gitlab-review\s[^\n]*--review-file" + _FLAG_TRAIL)),
    ("gitlab-review --reply-thread",
     re.compile(_BOUNDARY + r"gitlab-review\s[^\n]*--reply-thread" + _FLAG_TRAIL)),
    ("github-review --review-file",
     re.compile(_BOUNDARY + r"github-review\s[^\n]*--review-file" + _FLAG_TRAIL)),
    ("work state set --status=complete",
     re.compile(_BOUNDARY + r"work\s+state\s+set\s[^\n]*--status(?:=|\s+)complete" + _TRAIL)),
)

# A signal invocation. Matched loosely on purpose: any command that runs
# `lmer-signal` at all counts, because the message body is the agent's and the
# guard only cares that the channel was used. Quote-stripped like the milestone
# patterns — a mention inside a quoted string
# (`echo "then run lmer-signal when done"`) would otherwise clear a genuinely
# pending milestone, which is the expensive direction to be wrong in.
_SIGNAL_RE = re.compile(_BOUNDARY + r"lmer-signal" + _TRAIL)

# Marker key for the facts-side milestone. Distinct from any transcript key
# (those are tool_use ids) so a run reporting itself complete costs exactly
# one nudge no matter how many stops follow.
FACT_MILESTONE_KEY = "fact:completed_run"
FACT_MILESTONE_LABEL = "the run record reports itself complete"


# ---------------------------------------------------------------------------
# Pure logic — unit-testable seams. No env reads, no filesystem, no
# subprocesses; every input is injected by the caller.
# ---------------------------------------------------------------------------


def env_flag(value: str | None, default: bool = True) -> bool:
    """
    Parse a boolean env-var *value* with ``get_bool_env`` semantics.

    Truthy ``1/yes/true``, falsy ``0/no/false`` (case-insensitive); unset,
    empty, or unrecognized values return *default*. The kill switch is "unset
    or truthy enables", so its default is True.
    """
    text = (value or "").strip().lower()
    if not text:
        return default
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def parse_resume_json(stdout: str) -> dict | None:
    """
    Extract the decision object from ``work resume --json`` stdout.

    The command prints a single JSON line today; stray leading output is
    tolerated by falling back to the last JSON-parsable line. ``None`` when no
    object can be recovered (e.g. the "No run context" message) — the caller
    fails open.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        decision = json.loads(text)
        return decision if isinstance(decision, dict) else None
    except (ValueError, TypeError):
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            decision = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(decision, dict):
            return decision
    return None


def _command_of(block: dict) -> str | None:
    """Extract the shell command from a Bash ``tool_use`` block, if any."""
    if block.get("name") != "Bash":
        return None
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) else None


def _scannable(command: str) -> str:
    """Normalize a shell command for pattern matching.

    Joins backslash line continuations (the patterns are single-line, and the
    identifying flag is often on a later line of a wrapped invocation), then
    blanks out quoted spans so a *mention* is not an invocation. See
    _QUOTED_SPAN_RE for what the second step costs on unbalanced quotes.
    """
    return _QUOTED_SPAN_RE.sub(" ", _CONTINUATION_RE.sub(" ", command))


def _milestone_label(command: str) -> str | None:
    """The label of the first milestone pattern *command* matches, if any."""
    if not command:
        return None
    scanned = _scannable(command)
    for label, pattern in _MILESTONE_PATTERNS:
        if pattern.search(scanned):
            return label
    return None


def _is_signal(command: str) -> bool:
    """Whether *command* invokes ``lmer-signal`` (quoted mentions excluded)."""
    return bool(command) and bool(_SIGNAL_RE.search(_scannable(command)))


def iter_messages(transcript_path: str) -> list[dict]:
    """
    Parse a Claude Code transcript JSONL file into a list of event objects.

    Malformed lines are skipped rather than raising — a single bad line must
    not disable the guard for an otherwise healthy session.
    """
    events: list[dict] = []
    with open(transcript_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return events


def _tool_result_errors(events: list[dict]) -> dict:
    """
    Map ``tool_use_id`` -> ``is_error`` (bool) across all tool results.

    A Bash ``tool_result`` carries ``is_error: True`` when the command exited
    non-zero — how a milestone that actually happened is told from one that
    failed, and a delivered signal from a failed one.
    """
    errors: dict = {}
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if tool_use_id is not None:
                    errors[tool_use_id] = bool(block.get("is_error"))
    return errors


def walk_transcript(events: list[dict]) -> dict:
    """
    Walk the transcript once and report the three things the caller needs:

    ``{"pending": (label, marker key) | None, "ends_signalled": bool,
    "has_signal": bool}`` — the newest successful milestone command with no
    successful ``lmer-signal`` after it, whether the transcript ends in a
    signalled state (at least one successful signal, with nothing pending after
    it), and whether it holds any successful signal at all.

    This is the slack_reply_guard "since the last X" walk, which gets "this
    turn" without parsing turn boundaries: events are walked in order, a
    milestone becomes pending, and a signal clears whatever is pending. So
    milestone-then-signal ends silent while signal-then-milestone stays
    pending — the second is exactly the run that signalled its previous
    milestone and then produced another one.

    ``ends_signalled`` is what the facts side needs. A completed-run *fact*
    persists for the rest of the session, while the channel-dir signal
    suppressor is deliberately bounded to one stop, so without this the stop
    after a signalled completion would nudge for a signal that was already
    sent — and teach double-signalling, which is exactly the degradation of the
    channel this hook is built to avoid.

    ``has_signal`` decides whether the channel-dir signal file is *independent*
    evidence. When the transcript already shows a signal, the file is that same
    signal seen a second time, and letting it suppress would put unordered
    evidence (a file with no position in the turn) above ordered evidence (the
    walk), silencing a milestone that came *after* the signal. So the caller
    applies the channel suppressor only when this is False.

    The two ``is_error`` reads fail in opposite directions on purpose. A
    milestone whose result cannot be correlated counts as *succeeded*: a real
    milestone must not be dropped silently, and the cost of being wrong is one
    reminder. A signal whose result cannot be correlated counts as
    *delivered*: the cost of being wrong there is nagging a session that did
    the right thing.
    """
    errors = _tool_result_errors(events)
    pending: tuple[str, str] | None = None
    signalled = False
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            command = _command_of(block)
            if not command:
                continue
            if _is_signal(command):
                if errors.get(block.get("id")) is not True:
                    pending = None
                    signalled = True
                continue
            label = _milestone_label(command)
            if label and errors.get(block.get("id")) is not True:
                # Keyed on the tool_use id so each distinct milestone gets its
                # own nudge; a block without an id falls back to the pattern
                # label, which merges repeats of that command into one nudge.
                tool_use_id = block.get("id")
                key = tool_use_id if isinstance(tool_use_id, str) and tool_use_id \
                    else f"pattern:{label}"
                pending = (label, key)
    return {
        "pending": pending,
        "ends_signalled": signalled and pending is None,
        "has_signal": signalled,
    }


def unsignalled_milestone(events: list[dict]) -> tuple[str, str] | None:
    """The pending milestone from :func:`walk_transcript`, or ``None``."""
    return walk_transcript(events)["pending"]


def transcript_ends_signalled(events: list[dict]) -> bool:
    """Whether the transcript ends signalled (:func:`walk_transcript`)."""
    return walk_transcript(events)["ends_signalled"]


def transcript_has_signal(events: list[dict]) -> bool:
    """Whether the transcript holds any successful signal
    (:func:`walk_transcript`)."""
    return walk_transcript(events)["has_signal"]


def fact_milestone(decision: dict | None) -> tuple[str, str] | None:
    """
    Facts-side milestone from ``work resume --json``: the run reporting itself
    complete, as ``(label, marker key)``.

    ``completed_run`` is derived server-side by the work CLI (``status`` in
    complete/archived); ``status == "complete"`` is read too so an older CLI
    that predates the derived field still trips. Anything else — including a
    decision the guard could not parse — is no evidence, which fails open.
    """
    if not isinstance(decision, dict):
        return None
    if decision.get("completed_run") is True or decision.get("status") == "complete":
        return (FACT_MILESTONE_LABEL, FACT_MILESTONE_KEY)
    return None


def newest_signal_id(names: list[str]) -> int | None:
    """
    Highest numeric id among the channel dir's signal files; ``None`` when it
    holds none.

    Ids are zero-padded digits allocated in one sequence for the whole channel
    (src/ask_channel/protocol.py), so "greater than" is "later than". A file
    whose stem is not numeric is ignored rather than guessed at.
    """
    newest: int | None = None
    for name in names:
        if not name.endswith(SIGNAL_SUFFIX):
            continue
        stem = name[: -len(SIGNAL_SUFFIX)]
        try:
            value = int(stem)
        except (TypeError, ValueError):
            continue
        if newest is None or value > newest:
            newest = value
    return newest


def newest_open_question_id(names: list[str]) -> int | None:
    """
    Highest id among questions that are still open: a ``NNNNNN.question.json``
    with neither an ``.answer.json`` nor a ``.closed.json`` sidecar. ``None``
    when the channel dir holds no open question.

    An open question is signal-equivalent — the ask channel already told the
    orchestrator this run is waiting — so it suppresses the nudge. Both
    sidecars end that: an answered question no longer holds the orchestrator's
    attention, and a closed one notifies nobody, so neither suppresses.

    The *id* rather than a bool because the suppression has to be bounded the
    same way the signal one is (see :func:`signal_is_new`). A question can stay
    open for a long time — the prompt tells the agent to keep working while it
    waits — so a bare "any open question" test silenced every milestone for the
    rest of the session. Comparing against a recorded baseline suppresses the
    stops right after the question is posted and lets later, newer work nudge.

    Deliberately a file check and never the transcript: ``lmer-ask ask`` exits
    2 while a question is posted and waiting, which reads as ``is_error`` and
    would make the healthy case look like a failure.
    """
    present = set(names)
    newest: int | None = None
    for name in names:
        if not name.endswith(QUESTION_SUFFIX):
            continue
        stem = name[: -len(QUESTION_SUFFIX)]
        if stem + ANSWER_SUFFIX in present or stem + CLOSED_SUFFIX in present:
            continue
        try:
            value = int(stem)
        except (TypeError, ValueError):
            continue
        if newest is None or value > newest:
            newest = value
    return newest


def has_open_question(names: list[str]) -> bool:
    """Whether any question in the channel dir is still open."""
    return newest_open_question_id(names) is not None


def signal_is_new(newest_id: int | None, baseline: int | None) -> bool:
    """
    Whether the channel dir shows a signal the guard has not accounted for.

    Covers signals sent through a wrapper the transcript regex would miss.
    With no baseline recorded yet (the session's first evaluated stop) *any*
    signal file counts as new: first-stop conservatism, chosen because the
    alternative nags a session that has already reported itself.

    "New" is relative to the baseline the caller *advances on every evaluated
    stop*, so one channel-dir signal suppresses exactly the first stop that
    sees it and no later one. That bound is the whole reason this suppressor is
    safe: a signal id that stayed above a frozen baseline would silence every
    milestone for the rest of the session, which is the silent miss the hook
    exists to catch.

    The bound holds only while the marker does. The marker is container-local
    (``/tmp``, keyed on the session id) while the channel dir is a host mount
    that outlives any container, so a channel carried into a *new* session
    starts with no baseline and its newest existing signal suppresses that
    session's first evaluated stop. One stop, and only because a stale
    suppression is cheaper than a false nudge — but it is a limit of the
    marker's lifetime, not a property of the ids.

    ``lmer-signal`` is also not the only writer of these files, which is the
    other reason this suppressor is secondary to the transcript walk: the caller
    applies it only when the transcript shows no signal of its own.
    """
    if newest_id is None:
        return False
    if baseline is None:
        return True
    return newest_id > baseline


def advanced_baseline(baseline: int | None, newest_id: int | None) -> int | None:
    """
    The baseline to persist after a stop: monotonic, never regressing.

    Both write sites go through this. A baseline that moved backwards — to
    ``None`` because the channel dir currently lists no signal, or to a lower
    id — would make an already-accounted signal look new again and re-suppress
    a later milestone, which is the bug this function exists to make
    unrepresentable.
    """
    if newest_id is None:
        return baseline
    if baseline is None or newest_id > baseline:
        return newest_id
    return baseline


def build_reason(label: str, from_transcript: bool = True) -> str:
    """The message fed back to the agent when the guard blocks a stop.

    Two openings, because the two kinds of evidence support different claims.
    A transcript milestone happened in the turn that is ending. A completed-run
    *fact* may have been recorded by an earlier session — all this hook knows is
    that the record says complete and no signal was sent, so the facts wording
    says exactly that and does not claim "this turn".
    """
    if from_transcript:
        opening = (
            f"this turn completed reportable work ({label}) but the "
            "orchestrator was not signalled — it will otherwise learn this "
            "only from a stalled-run digest, up to an hour from now."
        )
    else:
        opening = (
            f"{label} but no signal was sent this session — the orchestrator "
            "will otherwise learn this only from a stalled-run digest, up to "
            "an hour from now."
        )
    return (
        f"Signal check: {opening} If you are idle on completed work — review "
        "round posted, MR pushed, fixes landed, run complete — signal now: "
        '`lmer-signal "<what happened>"`. If you are mid-task and stopping for '
        "another reason, just stop again — do not signal non-milestones."
    )


def evaluate(
    *,
    transcript_milestone: tuple[str, str] | None,
    fact_milestone: tuple[str, str] | None,
    question_seen: bool,
    signal_seen: bool,
    transcript_has_signal: bool = False,
    nudged_keys: tuple | list = (),
    nudges: int = 0,
    cap: int = NUDGE_CAP,
) -> dict:
    """
    Combine evidence, suppressors, and bookkeeping into one decision from
    fully injected inputs.

    Returns ``{"reason": str | None, "key": str | None, "accounted_key":
    str | None}``: the block reason, the marker key it must be recorded under
    (so the caller can drop the nudge if that bookkeeping write fails), and —
    when a *channel-dir signal* suppressed a pending transcript milestone — the
    key of that milestone, which the caller records as accounted-for so the next
    stop does not nudge for work a signal already covered.

    Suppressor precedence, in the order that matters:

    * ``question_seen`` and the channel signal suppress everything.
    * The channel signal only counts when ``transcript_has_signal`` is False.
      With a signal in the transcript, the walk has already placed it relative
      to the milestone; the file is the same event without a position, and
      letting it override would silence a milestone that came after the signal.
    * The transcript milestone is preferred over the facts one because it names
      the command the agent just ran.

    Both the per-milestone key and the session cap are checked here so the nudge
    budget is visible in one place.
    """
    channel_signal = signal_seen and not transcript_has_signal
    if question_seen or channel_signal:
        accounted = None
        if channel_signal and transcript_milestone:
            accounted = transcript_milestone[1]
        return {"reason": None, "key": None, "accounted_key": accounted}

    milestone = transcript_milestone or fact_milestone
    if milestone is None:
        return {"reason": None, "key": None, "accounted_key": None}

    label, key = milestone
    if key in tuple(nudged_keys):
        return {"reason": None, "key": None, "accounted_key": None}
    if nudges >= cap:
        return {"reason": None, "key": None, "accounted_key": None}
    return {
        "reason": build_reason(label, from_transcript=transcript_milestone is not None),
        "key": key,
        "accounted_key": None,
    }


# ---------------------------------------------------------------------------
# Impure gatherers — every one fails open (None / empty) on any error.
# ---------------------------------------------------------------------------


def list_channel_dir(ask_dir: str) -> list[str] | None:
    """Filenames in the channel dir; ``None`` when it cannot be listed.

    ``None`` means the suppressors are unverifiable, and the caller treats
    that as "do not nudge" — a nudge that cannot rule out an already-sent
    signal is the noise this guard exists to avoid.
    """
    try:
        return os.listdir(ask_dir)
    except OSError:
        return None


def read_marker(path: str) -> dict | None:
    """
    Marker state for this session: ``{}`` when absent, ``None`` when present
    but unreadable/corrupt.

    ``None`` suppresses the nudge (the same direction as run_state_guard's
    unreadable counter): without the counter the guard cannot honour its own
    cap, and an uncapped reminder is worse than a missed one.
    """
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            state = json.loads(fh.read() or "{}")
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def write_marker(
    path: str,
    keys: list,
    nudges: int,
    baseline: int | None,
    question_baseline: int | None = None,
) -> bool:
    """Persist marker state atomically; False on any failure (caller fails open).

    Write-to-temp-then-``os.replace`` rather than truncate-in-place. A partially
    written marker parses as corrupt, and :func:`read_marker` answers ``None`` to
    corrupt — which disables the guard for the rest of the session. Two hooks
    firing at once (or a container stopped mid-write) is enough to produce that,
    and the atomic swap makes the state unreachable: a reader sees either the old
    marker or the new one.
    """
    payload = {
        "keys": keys,
        # The cap's budget, kept separate from the dedupe list: `keys` answers
        # "was this milestone nudged", `nudges` answers "has this session
        # spent its reminders".
        "nudges": nudges,
        "signal_baseline": baseline,
        "question_baseline": question_baseline,
    }
    # Same directory as the target, so the replace is a rename within one
    # filesystem; pid-suffixed so concurrent writers do not share a temp file.
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False


def _run_work_resume() -> dict | None:
    """``work resume --json`` (read-only), parsed; None on any failure."""
    try:
        result = subprocess.run(
            ["work", "resume", "--json"],
            capture_output=True,
            text=True,
            timeout=WORK_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return parse_resume_json(result.stdout)


def main(argv: list[str] | None = None) -> int:
    """
    Stop-hook entrypoint. Reads the hook payload from stdin and, in an
    orchestrated session, blocks the stop once when the turn shows an
    unreported milestone.

    Always returns 0: blocking is signalled via the JSON ``decision`` field on
    stdout, never via exit code, and every failure path falls open.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return 0

    # Kill switch: unset or truthy enables; LMER_SIGNAL_GUARD=0 disables.
    if not env_flag(os.environ.get("LMER_SIGNAL_GUARD")):
        return 0

    # Fan-out children are skipped entirely. `spawn-harness` sets
    # LMER_NONINTERACTIVE=1 on every child it launches, and a `claude -p`
    # child's ONLY output is its last turn: a Stop block replaces that output
    # with the block reason, silently destroying the result its parent is
    # waiting for (measured twice in this project). A child also has no
    # orchestrator relationship of its own — it reports to the parent session,
    # which owns the signalling — so there is nothing here to remind it of.
    if env_flag(os.environ.get("LMER_NONINTERACTIVE"), default=False):
        return 0

    # Gate: an unorchestrated session has no orchestrator to signal, and no
    # channel dir to read the suppressors from.
    ask_dir = os.environ.get(ASK_DIR_ENV, "").strip()
    if not ask_dir or not os.path.isdir(ask_dir):
        return 0

    session = os.environ.get("LMER_SESSION_ID", "").strip() or "unknown"
    marker_path = MARKER_TEMPLATE.format(session=session)
    marker = read_marker(marker_path)
    if marker is None:
        return 0  # cap unenforceable — fail open

    names = list_channel_dir(ask_dir)
    if names is None:
        return 0  # suppressors unverifiable — fail open

    keys = marker.get("keys") if isinstance(marker.get("keys"), list) else []
    nudges = marker.get("nudges") if isinstance(marker.get("nudges"), int) else 0
    baseline = marker.get("signal_baseline")
    if not isinstance(baseline, int):
        baseline = None
    question_baseline = marker.get("question_baseline")
    if not isinstance(question_baseline, int):
        question_baseline = None

    newest_signal = newest_signal_id(names)
    newest_question = newest_open_question_id(names)
    signal_seen = signal_is_new(newest_signal, baseline)
    question_seen = signal_is_new(newest_question, question_baseline)

    def record(accounted_keys: list, spent: int) -> bool:
        """Persist the marker with both baselines advanced monotonically.

        A stop that changes nothing writes nothing (True without touching the
        file) — an unorchestrated-looking session should not leave a marker per
        yield. The nudge path always changes ``spent``, so it always writes and
        its return value still decides whether the block is allowed to happen.
        """
        next_signal = advanced_baseline(baseline, newest_signal)
        next_question = advanced_baseline(question_baseline, newest_question)
        if (
            next_signal == baseline
            and next_question == question_baseline
            and accounted_keys == keys
            and spent == nudges
        ):
            return True
        return write_marker(
            marker_path, accounted_keys, spent, next_signal, next_question,
        )

    # Already continuing from a previous nudge: never block again (no loops),
    # but the bookkeeping still runs. This stop is precisely the one *after* a
    # successful nudge — block, the agent signals, it stops again — so the
    # signal that nudge produced is visible here and nowhere else. Returning
    # early left it unaccounted, and it then read as "new" one stop later and
    # suppressed a genuinely new milestone.
    if payload.get("stop_hook_active"):
        record(keys, nudges)
        return 0

    transcript_milestone = None
    ends_signalled = False
    has_signal = False
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path.strip():
        try:
            walk = walk_transcript(iter_messages(transcript_path))
            transcript_milestone = walk["pending"]
            ends_signalled = walk["ends_signalled"]
            has_signal = walk["has_signal"]
        except Exception:
            transcript_milestone = None
            ends_signalled = False
            has_signal = False

    # Facts side: corroboration for milestones the patterns cannot see (a run
    # completed through a wrapper, a resumed session). Only worth a subprocess
    # when nothing has decided the outcome yet — and skipped outright once the
    # transcript ends signalled, because the completed-run fact persists for the
    # whole session while the channel-dir suppressor covers only one stop: the
    # stop after a signalled completion would otherwise be told to signal again.
    facts_milestone = None
    if not transcript_milestone and not signal_seen and not question_seen:
        if not ends_signalled and shutil.which("work") is not None:
            facts_milestone = fact_milestone(_run_work_resume())

    verdict = evaluate(
        transcript_milestone=transcript_milestone,
        fact_milestone=facts_milestone,
        question_seen=question_seen,
        signal_seen=signal_seen,
        transcript_has_signal=has_signal,
        nudged_keys=keys,
        nudges=nudges,
    )

    if not verdict["reason"]:
        # Nothing to say, but the bookkeeping still runs.
        #
        # Both baselines advance to what the guard just saw, on EVERY such stop
        # and not only the first: a baseline left behind keeps its signal (or
        # its open question) "newer" forever, so one of either would suppress
        # every later milestone in the session — the silent miss this hook
        # exists to catch.
        #
        # And when a channel-dir signal is what suppressed a *pending*
        # milestone, that milestone is marked accounted-for. Without it the
        # suppression expires with the baseline while the milestone stays in the
        # transcript, so the next stop would nudge for work a signal already
        # covered. All best-effort: a failed write only means the next stop
        # re-decides from the older marker.
        accounted = list(keys)
        if verdict["accounted_key"] and verdict["accounted_key"] not in accounted:
            accounted.append(verdict["accounted_key"])
        record(accounted, nudges)
        return 0

    # Bookkeeping BEFORE blocking: a nudge whose marker cannot be recorded
    # would repeat on every stop, so it is dropped instead (fail open).
    if not record(keys + [verdict["key"]], nudges + 1):
        return 0

    json.dump({"decision": "block", "reason": verdict["reason"]}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
