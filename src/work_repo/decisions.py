"""A run's recorded decisions: who decided what, at what authority (#674).

Who decided is stamped from the session bus record at write time, never typed;
docs/RUN-STATE.md "Decisions" has the rules and their limits.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from lmer_cli import comms

from . import run_state
from .utils import redact_secrets

DECISION_EVENT_TYPES = ("goals_frozen", "goal_amended", "question_answered", "decision")

BY_HUMAN = "human"
BY_ASSISTANT = "assistant"
BY_AGENT = "agent"
#: Someone on a chat thread who may not be a human: another lmer run, the
#: platform's post button, a bot, or a connector too old to say.
BY_UNKNOWN = "unknown"
_BY = (BY_HUMAN, BY_ASSISTANT, BY_AGENT, BY_UNKNOWN)

#: Channels only a human types into; a Slack or Matrix record is a human's only
#: when its connector says so (``meta.poster``).
_HUMAN_CHANNELS = (comms.CHANNEL_TERMINAL, comms.CHANNEL_PLATFORM)
_THREAD_CHANNELS = (comms.CHANNEL_SLACK, comms.CHANNEL_MATRIX)

#: Plain-text listing: one decision per line, the question's first line cut here.
TEXT_WIDTH = 200


class DecisionError(Exception):
    """A decision cannot be recorded from the given reference."""


# -- writing ------------------------------------------------------------------


def _authority(channel: object, stamp: object) -> str:
    """Only the daemon stamps authority, and only on the assistant's own
    channel; anything else carries none (#594). Writer and reader both ask here."""
    if channel != comms.CHANNEL_UBER or stamp not in comms.SUPERVISOR_AUTHORITY_LEVELS:
        return comms.AUTHORITY_NONE
    return stamp


def _who(record: dict) -> dict:
    """``by``/``channel``/``author``/``authority``/``label`` for a bus record."""
    source = record.get("source") or {}
    meta = record.get("meta") or {}
    channel = source.get("channel") or ""
    # An assistant session's own posts ride its assistant channel, marked as
    # the session's (comms.py), so the channel alone does not tell.
    if meta.get("session_origin") or channel in (comms.CHANNEL_AGENT, comms.CHANNEL_SUPERVISOR, ""):
        raise DecisionError(
            f"record {record.get('id')} is this session's own ({channel or 'no channel'}), "
            "not someone else's decision — record your own with `work decision \"<what>\"`"
        )
    who = {"channel": channel, "author": source.get("author")}
    if source.get("ref"):
        # Where a connector or the ask directory says the record came from.
        who["source_ref"] = source["ref"]
    if channel in comms.ASSISTANT_CHANNELS:
        label = comms.assistant_display_name() if channel == comms.CHANNEL_UBER else "peer assistant"
        who.update(by=BY_ASSISTANT, authority=_authority(channel, meta.get(comms.META_AUTHORITY)), label=label)
        return who
    human = channel in _HUMAN_CHANNELS or (
        channel in _THREAD_CHANNELS and meta.get(comms.META_POSTER) == comms.POSTER_HUMAN
    )
    who.update(by=BY_HUMAN if human else BY_UNKNOWN, authority=None,
               label=comms.CHANNEL_LABELS.get(channel, channel))
    if not human and meta.get(comms.META_POSTER):
        who["poster"] = meta[comms.META_POSTER]
    return who


def _bus_record(client, ref: str) -> dict:
    """The bus record *ref* names (a seq, padded or not, or a full id of
    this session — another session's id never resolves to a local record)."""
    text = str(ref or "").strip()
    seq = comms.parse_message_ref(text, text.rpartition("/")[0])
    if seq is None:
        raise DecisionError(f"{ref!r} is not a message id")
    messages, _cursor = client.messages(seq - 1)
    for record in messages:
        if record.get("seq") == seq:
            if "/" in text and record.get("id") != text:
                break
            return record
    raise DecisionError(f"no message {ref} on this session's bus")


def event_from_bus(ref: str, what: Optional[str], client=None) -> tuple[str, Optional[str], dict]:
    """``(event type, note, data)`` for a decision taken from bus record *ref*.

    An accepted answer, or a question named by its id, becomes
    ``question_answered`` with its question; any other record from a human or
    an assistant becomes ``decision``. A late
    answer was never accepted, so it is refused rather than recorded as one.
    """
    # deferred: import cost — ask_channel.bus adds ~140 ms beyond comms; only `work decision --ref` needs it
    from ask_channel import bus

    try:
        if client is None:
            client = bus.BusClient(bus.resolve_endpoint())
        return _event_from_record(client, _bus_record(client, ref), ref, what)
    except bus.BusError as exc:
        raise DecisionError(f"could not read message {ref} from the session bus: {exc}") from exc


def _event_from_record(client, record: dict, ref: str, what: Optional[str]) -> tuple[str, Optional[str], dict]:
    if record.get("kind") == "question":
        # The agent knows its question's id; the decision is the accepted answer.
        answered_by = (client.question(record.get("id") or "") or {}).get("answered_by")
        if not answered_by:
            raise DecisionError(f"question {ref} has no accepted answer")
        record = _bus_record(client, answered_by)
    who = _who(record)
    data = {"ref": record.get("id"), **who}
    if record.get("kind") == "answer":
        if (record.get("meta") or {}).get("late"):
            raise DecisionError(f"message {ref} is a late answer; the question had already been settled")
        state = client.question(record.get("in_reply_to") or "") or {}
        question = state.get("question") or {}
        data.update(
            question=redact_secrets(question.get("text")) if question.get("text") else None,
            question_ref=record.get("in_reply_to"),
            answer=redact_secrets(record.get("text") or ""),
            operator_only=bool((question.get("meta") or {}).get("operator_only")),
        )
        note = redact_secrets(what) if what else data["answer"]
        return "question_answered", note, data
    if record.get("kind") != "message":
        # Notes and closes on a human channel are often the system's own
        # (connector status, the supervisor's closes), never someone's decision.
        raise DecisionError(f"message {ref} is a {record.get('kind')}, not a message someone sent")
    if not what:
        raise DecisionError("say what was decided: work decision \"<what>\" --ref <id>")
    data["message"] = redact_secrets(record.get("text") or "")
    return "decision", redact_secrets(what), data


def agent_decision(what: str) -> tuple[str, str, dict]:
    """``(event type, note, data)`` for a decision the agent made on its own."""
    return "decision", redact_secrets(what), {"by": BY_AGENT}


# -- reading ------------------------------------------------------------------


def normalize_target(url: Optional[str]) -> str:
    """One spelling per target: GitLab serves an issue at both ``/-/issues/``
    and ``/-/work_items/``, and a trailing slash or fragment is not a
    different target."""
    text = (url or "").strip().split("#", 1)[0].rstrip("/")
    return text.replace("/-/work_items/", "/-/issues/")


def runs_for_target(target: str, base: Optional[Path] = None) -> list[tuple[Path, dict]]:
    """Every run record whose recorded target is *target*, in scan order."""
    wanted = normalize_target(target)
    found = []
    for rdir in run_state.iter_record_dirs(base):
        state = run_state.read_state_best_effort(rdir)
        if state and normalize_target(state.get("target")) == wanted:
            found.append((rdir, state))
    return found


def _run_label(rdir: Path, state: dict) -> str:
    """The run's name; in schema form also the execution, since every
    execution of one run shares its name."""
    label = str(state.get("name") or state.get("slug") or rdir.name)
    if state.get("schema") == 2:
        return f"{label}/{run_state.session_id_of(rdir.name)}"
    return label


def _goal_change(change: dict) -> str:
    def text(goal):
        return (goal or {}).get("text") or ""

    kind = change.get("change")
    if kind == "modified" and text(change.get("old")) != text(change.get("new")):
        return f'{change.get("id")} "{text(change.get("old"))}" → "{text(change.get("new"))}"'
    return f'{change.get("id")} {kind}: "{text(change.get("new"))}"'


def _what(event: dict) -> str:
    kind = event.get("type")
    note = event.get("note") or ""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if kind == "goals_frozen":
        return "Goals frozen (spec approved)" + (f": {note}" if note else "")
    if kind == "goal_amended":
        diff = data.get("diff") if isinstance(data.get("diff"), list) else []
        detail = note or "; ".join(_goal_change(c) for c in diff if isinstance(c, dict))
        return "Goals amended" + (f": {detail}" if detail else "")
    if kind == "question_answered":
        question = data.get("question")
        answer = data.get("answer") or note
        said = f"Q: {question} → A: {answer}" if question else f"A: {answer}"
        summary = _summary(event)
        return f"{summary} ({said})" if summary else said
    return note


def _summary(event: dict) -> Optional[str]:
    """The agent's own words for an answer, when it gave any beyond the answer."""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    note = event.get("note")
    return note if note and note != data.get("answer") else None


def _row(run: str, event: dict) -> dict:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    by = data.get("by") if data.get("by") in _BY else None
    answered = event.get("type") == "question_answered"
    return {
        "run": run,
        "ts": event.get("ts"),
        "type": event.get("type"),
        "what": _what(event),
        "question": data.get("question") if answered else None,
        "answer": (data.get("answer") or event.get("note")) if answered else None,
        "summary": _summary(event) if answered else None,
        "message": data.get("message"),
        "diff": data.get("diff") if event.get("type") == "goal_amended" else None,
        "by": by,
        "author": data.get("author") if by else None,
        "channel": data.get("channel") if by else None,
        "label": data.get("label") if by else None,
        "authority": _authority(data.get("channel"), data.get("authority")) if by == BY_ASSISTANT else None,
        "agent_only": by == BY_AGENT,
        "ref": data.get("ref"),
    }


def decision_rows(runs: Iterable[tuple[Path, dict]]) -> list[dict]:
    """The decisions recorded in *runs*, oldest first.

    Exact repeats collapse: `lmer-signal wait` re-run on an answered question
    records it again. A summarised answer lists once, with its summary.
    """
    rows = []
    seen = set()
    for rdir, state in runs:
        run = _run_label(rdir, state)
        for event in run_state.read_events(rdir, last_n=0):
            if event.get("type") not in DECISION_EVENT_TYPES:
                continue
            row = _row(run, event)
            if row["ref"]:
                key = (row["type"], row["ref"], event.get("note"))
                if key in seen:
                    continue
                seen.add(key)
            rows.append(row)
    summarised = {row["ref"] for row in rows if row["type"] == "question_answered" and row["summary"]}
    rows = [row for row in rows
            if not (row["type"] == "question_answered" and not row["summary"] and row["ref"] in summarised)]
    rows.sort(key=lambda row: str(row["ts"] or ""))
    return rows


def who_for(data: dict) -> str:
    """:func:`who_text` for one event's ``data`` (what a writer just recorded)."""
    return who_text(_row("", {"data": data}))


def who_text(row: dict) -> str:
    """Who decided, in the report's terms."""
    if row["by"] == BY_AGENT:
        return "the agent, on its own"
    if row["by"] == BY_ASSISTANT:
        return f"{row['label'] or 'assistant'} (assistant), authority={row['authority'] or 'none'}"
    if row["by"] == BY_HUMAN:
        author = row["author"] or "a human"
        return f"{author} via {row['label'] or row['channel']}"
    if row["by"] == BY_UNKNOWN:
        author = row["author"] or "someone"
        return f"{author} via {row['label'] or row['channel']}, not confirmed as a human"
    return "not recorded"


def _one_line(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= TEXT_WIDTH else text[: TEXT_WIDTH - 1] + "…"


def format_text(rows: list[dict]) -> str:
    if not rows:
        return "No decisions recorded."
    lines = []
    for row in rows:
        asked = next((line for line in str(row["question"] or "").splitlines() if line.strip()), None)
        if asked:
            # The answer is the decision; the question's first line says what it settled.
            what = f"A: {_one_line(row['answer'])} (to: {_one_line(asked)})"
            if row["summary"]:
                what = f"{_one_line(row['summary'])} — {what}"
        else:
            what = _one_line(row["what"])
            if row["message"]:
                # The note is the agent's summary; the message is what was said.
                what += f' (said: "{_one_line(row["message"])}")'
        mark = "AGENT-ONLY " if row["agent_only"] else ""
        day = str(row["ts"] or "")[:10]
        lines.append(f"- {mark}[{day} {row['run']}] {what} — {who_text(row)}")
    return "\n".join(lines)
