#!/usr/bin/env python3
"""Convert a live opencode session into the lmer transcript format (#296).

Runs *inside* the session container, backgrounded by ``runner.sh``, and appends
canonical records (``docs/TRANSCRIPT-FORMAT.md``) to
``~/.opencode-transcripts/<sessionID>.jsonl`` — the directory the manifest
declares as ``session_dir``, which the platform mounts out and reads back. The
daemon never runs this file; it only reads what it writes.

**Storage-agnostic on purpose.** opencode keeps its sessions in a SQLite
database whose schema is not a contract and whose live writer holds it, so this
polls opencode's own reader instead — ``opencode session list --format json``
to find the session, ``opencode export <sessionID>`` to dump it. Reading the
database directly (stdlib ``sqlite3``, read-only URI) would avoid the
subprocess, at the price of tracking a schema that migrates without notice and
contending with the writer's WAL. The CLI route is the recommended pattern.

**Delta-append, by part.** Each export is a full snapshot, but the canonical
file is strictly append-only (in-flight clients hold cursors into it), so every
poll emits only what it has not emitted before, and a tool call whose state
flipped since is resolved with an ``lmer.tool_update`` rather than by rewriting
the message that carried it.

The unit consumed is the *part*, not the message: an opencode message grows
while it is being answered — a second tool call, another paragraph — so a
message consumed whole on the poll that first saw it would lose everything
appended to it afterwards. Every record therefore carries where it came from:
``native_id`` (the opencode message) and ``native_parts`` (the half-open part
span it covers). Both are unknown fields to the reader, which ignores them, and
they are what lets a restarted converter — or the single ``--once`` pass of the
guaranteed-final-pass pattern — resume against a file it did not write in this
process, at the exact part the last one stopped at.

**One opencode message can become several ``lmer.message`` records**, on
different polls: a turn that says something, runs a tool, then says more is
three records, and a turn caught mid-answer is continued by the next poll. That
is correct — the format's unit is a turn's worth of content in file order, the
view concatenates them, and the alternative is holding turns back until they
settle, which is exactly the buffering the no-flush-window rule forbids.

**A part is consumed only when it is settled**, and settledness is read off the
part itself, never off where its message sits in the export. A part with a
successor is final (parts stream in order); a tool part is emitted immediately
as ``pending``, so the live "· running" chip appears and its outcome arrives
later as an ``lmer.tool_update``; a *user* message is settled whole, since its
parts arrive with the prompt rather than streaming. That leaves the one case the
rule is for — the trailing text part of an *assistant* message still streaming,
a paragraph mid-flight, which would be frozen truncated if converted now (the
format has no way to extend a record). It waits for its ``time.end`` — but not
forever: an interrupted stream arrives with no ``end`` and no ``completed``
ever, so the wait is bounded by :data:`STALLED_POLLS` polls of the message not
changing at all.

**Nothing here may cost the session.** Every poll is wrapped: a failed
``opencode`` invocation, a torn export, an output directory that does not exist
yet are all retried on the next poll. The process is orphaned to the container's
init and dies with the container, with no shutdown signal and no flush window —
hence the per-record flush.

Overridable so a test can run one deterministic pass (env var, or the argv flag
which wins):

===========================  ==============================  ================
env var                      flag                            default
===========================  ==============================  ================
``LMER_OPENCODE_BIN``        ``--opencode <path>``           ``opencode``
``LMER_OPENCODE_OUT_DIR``    ``--output-dir <dir>``          ``~/.opencode-transcripts``
``LMER_OPENCODE_INTERVAL``   ``--interval <seconds>``        ``5.0``
``LMER_OPENCODE_DIRECTORY``  ``--directory <dir>``           the working directory
``LMER_OPENCODE_ONCE``       ``--once``                      keep polling
===========================  ==============================  ================
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

#: What the ``lmer.meta`` header claims about this file. ``harness`` is what
#: labels the transcript's source in the API and UI, so it must match the
#: drop-in's directory name; the other two are provenance for whoever is
#: debugging a conversation that came out wrong.
HARNESS = "opencode"
GENERATOR = "opencode-converter/0.1"
NATIVE_FORMAT = "opencode-export/1"
FORMAT_VERSION = 1

#: opencode's four tool states (``ToolState`` in its own OpenAPI document),
#: mapped onto the three the canonical format has. Anything else — a state a
#: later opencode adds — reads as still running, which is the honest answer and
#: the one a later ``lmer.tool_update`` can still correct.
TOOL_STATUS = {
    "completed": "ok",
    "error": "failed",
    "running": "pending",
    "pending": "pending",
}

#: Where to look, in order, for the one line a tool chip shows about what a call
#: acted on. These are the input fields opencode's built-in tools carry (bash,
#: read/write/edit, grep/glob, task, webfetch); ``state.title`` is opencode's own
#: one-line summary and the fallback when a tool this list does not know about
#: has finished. An unknown *running* tool gets its input as compact JSON, since
#: a chip saying nothing at all invites the reader to guess.
DETAIL_KEYS = (
    "command", "filePath", "path", "pattern", "url", "description", "prompt",
)

#: One line, bounded like the host bounds it (``DETAIL_LIMIT``), so what is
#: written is what is shown.
DETAIL_LIMIT = 160

#: How many further polls a *held* trailing text part waits through, with its
#: message unchanged, before it is written anyway. Three, at the default
#: five-second interval, is a quarter of a minute of a session saying nothing at
#: all — while a live answer moves its trailing part every poll, and opencode
#: stamps ``time.end`` on it within milliseconds of it finishing (both probed).
#: Small enough that an interrupted turn reaches the chat view while it is still
#: interesting, large enough that a slow provider is never mistaken for a dead
#: one. The bound is what matters more than the number: without it, a stream cut
#: by an interrupt — which arrives with neither marker — is held for the rest of
#: the session.
STALLED_POLLS = 3

#: Part types that carry conversation. Everything else opencode writes —
#: ``step-start``, ``step-finish``, ``snapshot``, ``patch``, ``agent``,
#: ``reasoning``, and whatever a later release adds — is skipped: the format is
#: not this converter's contract, and an unrecognised part must cost itself
#: rather than the turn around it.
TEXT_PART = "text"
TOOL_PART = "tool"


def log(message):
    """Say something on stderr, which ``runner.sh`` redirects to a log file.

    Never on stdout: stdout is not where anything is read from, and a converter
    that writes to the session's terminal would be talking over the harness.
    """
    print("opencode-converter: %s" % message, file=sys.stderr, flush=True)


def iso(millis):
    """opencode's epoch-milliseconds as the ISO-8601 UTC string the format asks
    for, or ``None`` when there is no usable timestamp (the field is optional,
    and file order is conversation order regardless)."""
    if not isinstance(millis, (int, float)):
        return None
    moment = datetime.datetime.fromtimestamp(
        millis / 1000.0, datetime.timezone.utc
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (
        moment.microsecond // 1000
    )


def truthy(value):
    """Whether an env var *says* yes, rather than merely being set.

    The same allowlist the rest of lmer reads env flags with — so
    ``LMER_OPENCODE_ONCE=0`` means what it says instead of turning a tailer into
    a single pass because the variable exists.
    """
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def one_line(text):
    """A single bounded line of *text*, or ``None`` when there is none."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:DETAIL_LIMIT]
    return None


class Converter:
    """One opencode session, converted into one canonical file."""

    def __init__(self, opencode, out_dir, directory):
        self.opencode = opencode
        self.out_dir = out_dir
        self.directory = directory
        self.session_id = None
        self.path = None
        #: opencode message id → how many of its parts are already on disk.
        #: Per part rather than per message because a message grows after it is
        #: first seen, and because a write that fails halfway through a message
        #: must resume at the part it stopped at — re-emitting a turn into an
        #: append-only file is as wrong as dropping one. Recovered from the
        #: file's own records by :meth:`_resume`.
        self.consumed = {}
        #: callID → the status last written for it, so a state flip after the
        #: message went out becomes an ``lmer.tool_update`` and not a rewrite.
        self.tool_status = {}
        #: message id → ``[fingerprint, unchanged polls]`` for a message whose
        #: trailing text part is being held. Bounds the hold (see
        #: :data:`STALLED_POLLS`); in memory only, since a restart re-reads the
        #: export and can start counting again from what it finds.
        self.stalled = {}

    # -- opencode, as a subprocess -----------------------------------------

    def _run(self, *args):
        """One opencode invocation, parsed. ``None`` for every way of failing.

        Failure is ordinary here — the CLI is started before the session exists,
        the database is busy, the binary is mid-install — so it is logged at
        most once per kind and retried on the next poll.
        """
        try:
            done = subprocess.run(
                [self.opencode] + list(args),
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log("%s failed: %r" % (args[0], exc))
            return None
        if done.returncode != 0:
            log("%s exited %d" % (args[0], done.returncode))
            return None
        try:
            return json.loads(done.stdout)
        except ValueError:
            # A torn or partial dump, which a snapshot of a session being
            # written can be. The next poll sees a whole one.
            return None

    def _discover(self):
        """Pin the newest opencode session for this working directory.

        Pinned rather than re-read every poll: a subagent gets its own session,
        which becomes the *newest* the moment it runs, and following that would
        move the conversation into a second file mid-turn. If nothing claims
        this directory the newest session overall is taken — the container runs
        one session, and being wrong about the directory should not mean
        converting nothing.
        """
        sessions = self._run("session", "list", "--format", "json")
        if not isinstance(sessions, list) or not sessions:
            return None
        here = [s for s in sessions
                if isinstance(s, dict) and s.get("directory") == self.directory]
        if not here:
            log("no session for %s; falling back to the newest overall"
                % self.directory)
            here = [s for s in sessions if isinstance(s, dict)]
        if not here:
            return None
        newest = max(here, key=lambda s: s.get("updated") or s.get("created") or 0)
        session_id = newest.get("id")
        return session_id if isinstance(session_id, str) and session_id else None

    # -- the output file ---------------------------------------------------

    def _resume(self, path):
        """Recover what a previous run of this converter already wrote.

        The file is the state: a converter restarted mid-session (or a
        single-pass run following an earlier one) must not re-emit turns that
        are already on disk and already have client cursors pointing past them,
        and must not skip the ones a half-finished flush never reached. Both
        answers come from the same field — the ``native_parts`` span each record
        carries — read as "the highest part index this message has on disk".
        An unreadable line is skipped the way the reader skips it.

        Reports whether a record was actually *read*, not whether the file
        opened: a header write that failed leaves a zero-byte file behind (mode
        ``a`` creates before it writes), and reading that as "already headered"
        would spend the rest of the session appending turns to a file with no
        ``lmer.meta`` in it — which the API reads back labelled ``lmer`` instead
        of by this harness's name.
        """
        read_something = False
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return False
        except OSError as exc:
            log("cannot read %s: %r" % (path, exc))
            return False
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                read_something = True
                kind = record.get("type")
                if kind == "lmer.message":
                    native = record.get("native_id")
                    span = record.get("native_parts")
                    if isinstance(native, str) and isinstance(span, list) \
                            and len(span) == 2 and isinstance(span[1], int):
                        self.consumed[native] = max(
                            self.consumed.get(native, 0), span[1]
                        )
                    for tool in record.get("tools") or []:
                        if isinstance(tool, dict) and tool.get("id"):
                            self.tool_status[tool["id"]] = tool.get("status")
                elif kind == "lmer.tool_update":
                    if record.get("id"):
                        self.tool_status[record["id"]] = record.get("status")
        return read_something

    def _open(self, session_id):
        """Prepare the output file for *session_id*, writing the header once.

        ``session_dir`` is a symlink into the staged mount for a user harness
        and the linker is deliberately fail-soft, so this can find nothing to
        write into; that is a quiet retry, never an exit.
        """
        path = os.path.join(self.out_dir, "%s.jsonl" % session_id)
        try:
            os.makedirs(self.out_dir, exist_ok=True)
        except OSError as exc:
            log("cannot use %s: %r" % (self.out_dir, exc))
            return False
        self.path = path
        if self._resume(path):
            return True
        if self._append({
            "type": "lmer.meta", "format": FORMAT_VERSION, "harness": HARNESS,
            "generator": GENERATOR, "native_format": NATIVE_FORMAT,
        }):
            return True
        # Unopened, so the next poll writes the header before anything else: a
        # file whose first record is a turn reads back labelled ``lmer`` rather
        # than by this harness's name.
        self.path = None
        return False

    def _append(self, record):
        """One record, appended and flushed.

        Per record because there is no session end to flush at: the container
        goes away without warning the converter, so the loss window has to be
        the record in flight and nothing more.
        """
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError as exc:
            log("cannot append to %s: %r" % (self.path, exc))
            return False
        return True

    # -- opencode's shapes, in canonical terms -----------------------------

    def _tool(self, part):
        """One ``tools`` entry from an opencode tool part, or ``None``.

        A part with no tool name is dropped rather than named: the chip would
        say nothing, and the reader drops it anyway.
        """
        name = part.get("tool")
        if not isinstance(name, str) or not name.strip():
            return None
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        entry = {
            "id": part.get("callID"),
            "name": name.strip()[:DETAIL_LIMIT],
            "status": TOOL_STATUS.get(state.get("status"), "pending"),
        }
        detail = None
        source = state.get("input") if isinstance(state.get("input"), dict) else {}
        for key in DETAIL_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                detail = one_line(value)
                break
        if detail is None:
            detail = one_line(state.get("title")) if isinstance(
                state.get("title"), str) else None
        if detail is None and source:
            detail = one_line(json.dumps(source, ensure_ascii=False))
        if detail:
            entry["detail"] = detail
        if entry["status"] == "failed":
            error = state.get("error")
            error = one_line(error) if isinstance(error, str) else None
            if error:
                entry["error"] = error
        if entry["id"] is None:
            del entry["id"]
        return entry

    def _settled(self, message):
        """How many of *message*'s parts are final enough to convert.

        Everything up to the returned index can be read as written: opencode
        appends parts in order, so a part with a successor is finished. The only
        question is the *last* part, and it is answered from the part itself —
        never from the message's position in the export. "opencode writes one
        message at a time" is a prediction about a program, and both ways of
        being wrong about it cost words: a user turn withheld because a reply
        has not been persisted yet (a provider error, an interrupt, or a
        teardown makes that permanent — and it is the operator's own typed
        text), or an assistant paragraph frozen half-streamed because another
        message appeared beside it.
        A **user** message is never streamed — its parts arrive whole with the
        prompt, and its text parts carry no ``time`` at all — so all of it is
        settled the moment it exists.
        An **assistant** text part is settled when it carries ``time.end``,
        which opencode writes when the part stops growing (its own schema
        requires only ``start``). ``time.completed`` on the message says the
        same thing about all of them and is honored too: it can only ever
        settle *more*, and a completed message holding a part back forever is
        the one failure with no next poll to fix it.
        A trailing *tool* part is never held — a call that has started is a
        fact, and its outcome arrives through :meth:`_resolve`.

        **Neither marker is guaranteed to arrive**, so the hold is bounded by
        :data:`STALLED_POLLS`. Probed against opencode 1.18.18 on a slow
        streaming endpoint, three ways of cutting a stream mid-answer, exported
        afterwards:

        * *provider drops the connection* — the message gains ``time.completed``
          with ``finish: "unknown"`` and the part gains ``time.end`` around the
          partial text it did receive. Settles itself; nothing held.
        * *SIGINT to ``opencode run``* (the operator interrupting) — the message
          keeps only ``time.created`` and the trailing text part only
          ``time.start``. **Neither marker ever arrives**, and a message that
          stopped changing has no later poll that could change it.
        * *SIGKILL of the process group* (a container going away) — the
          assistant message is persisted with no parts at all. Nothing to hold.

        So the second shape is real, and an unbounded rule would hold that part
        for the rest of the session. What releases it is the message itself
        having stopped changing: :meth:`_stalled` counts polls in which nothing
        about it moved, and the hold ends after a few. That is the same
        truncation risk the old "release it because another message appeared"
        rule took — taken deliberately, from evidence about *this* message, and
        only once waiting has stopped being able to help.
        """
        parts = message.get("parts") or []
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        if info.get("role") != "assistant":
            return len(parts)
        native_id = info.get("id")
        if bool((info.get("time") or {}).get("completed")):
            self.stalled.pop(native_id, None)
            return len(parts)
        last = parts[-1] if parts else None
        if isinstance(last, dict) and last.get("type") == TEXT_PART \
                and not (last.get("time") or {}).get("end"):
            if self._stalled(native_id, parts, last):
                return len(parts)
            return len(parts) - 1
        self.stalled.pop(native_id, None)
        return len(parts)

    def _stalled(self, native_id, parts, last):
        """Whether a held part's message has stopped changing for good.

        The fingerprint is deliberately about *shape*, not content: how many
        parts the message has and how long its trailing text is. A message still
        being answered changes one of those between polls — a word arrives, a
        part is added, the text is flushed — so this cannot fire on a stream
        that is merely slow, only on one that is over without saying so.

        The count lives in this process, so a single ``--once`` pass never
        releases anything: one look is no evidence that anything has stopped,
        and releasing on it would freeze a paragraph that was simply mid-flight.
        What that costs the guaranteed-final-pass runner pattern is the turn in
        flight at teardown — the loss window that design already accepts, and
        the terminal log still has it.

        A release lands the fragment in the file *after* whatever was written
        while it was held, and file order is render order — so an interrupted
        reply reads as the newest thing said, below the prompt that followed
        it, carrying its own older ``at``. That ordering is the accepted price
        of the fallback: before it, the fragment never appeared at all, and a
        turn shown late beats a turn never shown.
        """
        if not isinstance(native_id, str):
            # Nothing to key the count on. The part is held; a message with no
            # id is not one this converter can emit anyway.
            return False
        fingerprint = (len(parts), len(last.get("text") or ""))
        seen = self.stalled.get(native_id)
        if seen is None or seen[0] != fingerprint:
            self.stalled[native_id] = [fingerprint, 0]
            return False
        seen[1] += 1
        return seen[1] >= STALLED_POLLS

    def _records(self, message, start, limit):
        """The canonical records ``parts[start:limit]`` becomes, in file order.

        Split by ``kind`` rather than one record per message: opencode marks
        *parts*, not messages, as ``synthetic`` — its own flag for text the
        machinery put in front of the model — and a turn that carries both an
        injection and something the operator typed has to render as two, or the
        injection reads as words a person said. That is the mistake this view
        keeps having to close, and the converter is the only place that knows.

        Each record carries the half-open part span it covers, so the state that
        survives this process is written down beside the content it describes.
        A span reaches back to where the previous record stopped, which is how
        the parts that convert to nothing — ``step-start``, ``step-finish``, a
        shape a later opencode adds — are consumed rather than reconsidered
        forever. Parts after the last content part stay unconsumed: they cost a
        re-read next poll and nothing else.
        """
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = info.get("role")
        if role not in ("user", "assistant"):
            # opencode has exactly these two; a third would be a shape this
            # converter has never seen, and guessing at it is how injected
            # content ends up drawn as an operator turn.
            return []

        at = iso((info.get("time") or {}).get("created"))
        native_id = info.get("id")
        parts = message.get("parts") or []
        segments = []
        span_start = start
        for index in range(start, min(limit, len(parts))):
            part = parts[index]
            if not isinstance(part, dict):
                continue
            kind_of_part = part.get("type")
            kind = None
            tool = None
            if kind_of_part == TEXT_PART:
                text = part.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                # opencode's own provenance, not a heuristic on the text:
                # ``synthetic`` is what it sets on context it injects (an
                # attachment read, a task hand-off, a compaction note), and
                # ``ignored`` marks a part it did not send to the model.
                kind = "injected" if (
                    part.get("synthetic") or part.get("ignored")) else "said"
            elif kind_of_part == TOOL_PART:
                tool = self._tool(part)
                if tool is None:
                    continue
                # A tool call is the assistant acting, never injected context,
                # so it never joins an injected segment.
                kind = "said"
            else:
                continue

            if segments and segments[-1]["kind"] == kind:
                segments[-1]["end"] = index + 1
            else:
                segments.append({
                    "kind": kind, "start": span_start, "end": index + 1,
                    "text": [], "tools": [],
                })
            if tool is not None:
                segments[-1]["tools"].append(tool)
            else:
                segments[-1]["text"].append(part["text"])
            span_start = index + 1

        records = []
        for segment in segments:
            record = {
                "type": "lmer.message",
                "role": role,
                "kind": segment["kind"],
                "text": "\n".join(segment["text"]),
            }
            if at:
                record["at"] = at
            if segment["tools"]:
                record["tools"] = segment["tools"]
            if isinstance(native_id, str):
                record["native_id"] = native_id
            record["native_parts"] = [segment["start"], segment["end"]]
            records.append(record)
        return records

    # -- one poll ----------------------------------------------------------

    def poll(self):
        """Convert whatever is new. Returns ``True`` when anything was written."""
        if self.session_id is None:
            self.session_id = self._discover()
            if self.session_id is None:
                return False
        if self.path is None:
            # Retried without re-discovering: the pinned session is still the
            # right one, and re-running discovery here would hand a directory
            # whose subagent has since started a session of its own the wrong
            # answer — the drift :meth:`_discover` pins against.
            if not self._open(self.session_id):
                return False

        export = self._run("export", self.session_id)
        if not isinstance(export, dict):
            return False
        messages = export.get("messages")
        if not isinstance(messages, list):
            return False

        wrote = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            info = message.get("info") if isinstance(
                message.get("info"), dict) else {}
            native_id = info.get("id")
            if not isinstance(native_id, str):
                # Nothing to record consumption against, so converting it would
                # mean re-emitting it on every poll.
                continue
            # An outcome that arrived after its call went out, first: the update
            # belongs behind the turn it resolves and in front of what came
            # after it.
            wrote = self._resolve(message) or wrote

            consumed = self.consumed.get(native_id, 0)
            limit = self._settled(message)
            if limit <= consumed:
                continue
            for record in self._records(message, consumed, limit):
                if not self._append(record):
                    # Consumption is recorded *after* the write, so a failure
                    # here leaves the next poll resuming at exactly the part
                    # this record covers: no turn twice, none skipped.
                    return wrote
                wrote = True
                self.consumed[native_id] = record["native_parts"][1]
                for tool in record.get("tools") or []:
                    if tool.get("id"):
                        self.tool_status[tool["id"]] = tool["status"]
        return wrote

    def _resolve(self, message):
        """Emit an ``lmer.tool_update`` for every call whose state has flipped."""
        wrote = False
        for part in message.get("parts") or []:
            if not isinstance(part, dict) or part.get("type") != TOOL_PART:
                continue
            call_id = part.get("callID")
            if not isinstance(call_id, str) or call_id not in self.tool_status:
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = TOOL_STATUS.get(state.get("status"), "pending")
            if status == self.tool_status[call_id] or status == "pending":
                continue
            record = {"type": "lmer.tool_update", "id": call_id, "status": status}
            error = state.get("error")
            error = one_line(error) if isinstance(error, str) else None
            if error:
                record["error"] = error
            if not self._append(record):
                return wrote
            self.tool_status[call_id] = status
            wrote = True
        return wrote


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Convert a live opencode session to the lmer transcript "
                    "format.")
    parser.add_argument(
        "--opencode", default=os.environ.get("LMER_OPENCODE_BIN", "opencode"),
        help="opencode binary to poll (default: opencode on PATH)")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get(
            "LMER_OPENCODE_OUT_DIR",
            os.path.join(os.path.expanduser("~"), ".opencode-transcripts")),
        help="where canonical files are written — the declared session_dir")
    parser.add_argument(
        "--interval", type=float,
        default=float(os.environ.get("LMER_OPENCODE_INTERVAL", "5.0")),
        help="seconds between polls (default: 5)")
    parser.add_argument(
        "--directory", default=os.environ.get("LMER_OPENCODE_DIRECTORY")
        or os.getcwd(),
        help="the opencode working directory whose session to convert")
    parser.add_argument(
        "--once", action="store_true",
        default=truthy(os.environ.get("LMER_OPENCODE_ONCE")),
        help="convert what exists now and exit (tests, and the "
             "guaranteed-final-pass runner pattern)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    converter = Converter(args.opencode, args.output_dir, args.directory)

    def poll():
        """One poll, which cannot fail loudly.

        The chat view is worth a poll; the session is not. Whatever went wrong
        — a shape opencode changed, a full disk — is logged and tried again
        rather than ending the process that is the only thing writing this
        file. The same in ``--once`` mode, where the caller is a runner shell
        that has a session to finish.
        """
        try:
            converter.poll()
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log("poll failed: %r" % exc)

    if args.once:
        poll()
        return 0
    while True:
        poll()
        time.sleep(max(args.interval, 0.1))


if __name__ == "__main__":
    sys.exit(main())
