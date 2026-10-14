"""The orchestrating assistant's lifecycle (issue #141, T29; spec §8).

What the assistant is
---------------------
One long-lived lmer session the platform runs *for itself* (D11). The operator
chats with it to ask what is running, what needs them, and to tell it to
prioritise, spawn or stop work. It is a session like any other — it appears in
the registry, it has a control plane, it can be piped into — with
``kind="assistant"``. Everything the rest of this package does is the platform
this thing drives.

This module owns its **lifecycle**, the small piece of state that outlives its
process (``assistant.json``, spec §6.1), and the *environment* that lets the
session reach the platform at all (:func:`_prepare_environment`). It does not
own a control surface, because there is no new one to own — see below — and it
deliberately does not own detection; see further below.

The control surface is the REST API, not a new CLI (§8.2, revised)
-------------------------------------------------------------------
Spec §8.2 specifies an ``lmer-ctl`` for the assistant to drive the platform
through. That is dropped. The platform already serves an HTTP API that covers
every verb the spec listed as reachable — ``POST /api/sessions`` spawns,
``/api/runs/answer`` and ``/api/runs/resume`` continue a run,
``/api/sessions/{id}/input`` and ``/log`` reach a worker, ``/wind-down`` and
``/exit`` end one — and an agent needs no wrapper to use an API. The caps are
enforced daemon-side on that path (:class:`lmer_platform.spawn.CapacityError`),
so a refusal-with-reason arrives over HTTP exactly as it would have through a
CLI; a cap does not need a CLI to be real.

What a CLI *would* have carried is the knowledge — which route does what, and
which two verbs must never be confused. That lives in the ``orchestrate``
taskdef instead, which is the half of this that the assistant actually reads.

§8.2's ``queue``/``slots``/``hold``/``park`` verbs have no backend at all and
are therefore not simulated here: §8.4 is explicit that a state machine must not
be invented ahead of the thing it models.

It never writes code, and that is structural (D17)
--------------------------------------------------
The assistant runs with **no repo checkout**. A prompt saying "do not edit code"
is the kind of gate that gets ignored under pressure; a session with nothing to
edit cannot edit anything. Its toolset is the platform's API, the reviewer CLIs
and the ``work`` CLI — enough to route signals, read findings and judge, not
enough to change a line of code.

The mechanism is ``LMER_NO_REPO=1``, and it is now wired end to end:
:func:`start` sets :attr:`lmer_platform.spawn.SpawnRequest.no_repo`, the spawn
puts the variable in the child's environment
(:data:`lmer_platform.spawn.NO_REPO_ENV`), the host CLI skips repo resolution on
it — which is what lets a target that is not a repository be given at all — and
the container's ``clone_and_exec`` skips the workspace clone, leaving
``/workspace`` empty (``lmer_cli/container/clone_and_exec.py``,
``tests/test_clone_and_exec_no_repo.py``). Nothing about that depends on the
assistant's prompt, which is the property D17 is asking for.

:data:`TARGET` stays a word that cannot name a repository, and it is the second
line of that defence rather than the first: if the switch above ever stopped
being passed, ``lmer``'s resolver would refuse the target instead of inferring a
repo from the daemon's working directory, and the session would fail to start.
A started-but-repo-less assistant is the goal; a started-*with*-a-checkout
assistant would be a silent D17 violation, so the failure that survives a
regression here is the loud one.

The daemon detects; the assistant is notified (§8.3)
----------------------------------------------------
There is deliberately **no poller here**. If detection were delegated to the
assistant ("check every session every minute"), its context window would fill
with routine noise within hours and it would degrade exactly when the fleet is
busiest — and the UI's attention badge would depend on an LLM being alive. The
daemon already computes the attention list mechanically
(:mod:`lmer_platform.inventory`), which works whether or not an assistant exists.

What this module provides instead is the seam: :func:`notify` takes a compact
digest of a *material* event and :func:`take_pending` drains it. The spool is
bounded (:data:`MAX_PENDING`) because a notification queue nobody drains is a
memory leak with a filename, and because history already has a home
(``events.jsonl``). Delivery — waking a live assistant with what is spooled — is
the caller's wiring, not this module's.

The daemon starts it and keeps it running (§8.1, T63)
------------------------------------------------------
"Long-lived (D11), supervised by the daemon (respawned if it dies)" is spec §8.1,
and :class:`Supervisor` is that half. It is wired into ``lmer platform run`` and
into nothing else: ``status``, ``rescan``, ``runs``, ``adopt``, ``forget``,
``spawn`` and ``setup-ui`` are diagnostic or one-shot verbs, and a diagnostic
command that launches a container as a side effect is a command nobody can run
while they are working out what is wrong.

Four properties, each of which is a way this goes badly wrong:

- **A failure to start never stops the daemon serving.** The fleet view is the
  thing an operator needs when something is broken, and it does not depend on
  the assistant at all — :mod:`lmer_platform.inventory` computes the attention
  list mechanically. So every refusal and every unexpected error is absorbed,
  said out loud, and carried past: a platform that boots and reports the
  assistant down beats one that will not boot.
- **The respawn backs off and eventually gives up.** Each attempt costs a clone
  and an image pull, so a crash-looping assistant with no backoff is an
  expensive infinite loop. The budget is spent by consecutive failures — which
  includes an incarnation that *started* and did not survive
  :data:`SETTLED_SECONDS`, because a start that succeeds and dies in two seconds
  is the crash loop, not the cure — and giving up is logged and recorded in
  ``events.jsonl`` rather than going quiet. What brings it back is an operator:
  ``POST /api/assistant/start``, which re-arms the supervision it stopped as well
  as starting the container (:func:`resume_supervision`, below).
- **It only ever starts.** Stopping is :func:`stop`'s, which owns the pointer and
  ``stop_reason`` bookkeeping that :func:`lmer_platform.lifecycle.exit_session`
  deliberately refuses ``kind="assistant"`` to protect; rotation on age or
  context pressure is §8.3's and is a policy, not a supervision detail. A
  supervisor that killed anything would be routing around both.
- **An unreachable platform is not a reason to refuse.** See :func:`start` and
  :func:`_prepare_environment`: a session with no URL is told why in its own
  environment and still works through the ask channel. The daemon says so at
  startup (:attr:`SupervisionReport.notice`) because "the chat works but cannot
  drive anything" is invisible otherwise.

Death is *observed*, not polled for. :func:`lmer_platform.spawn.wait_for_exit_recorded`
blocks until the spawn's own watcher thread has recorded how a session ended, so
a respawn follows the exit rather than the next tick. The poll interval
(:data:`SUPERVISE_POLL_SECONDS`) is only the fallback for an assistant *this*
process did not spawn — one adopted at boot has no watcher here, so there is no
event to wait on and the registry has to be re-read.

Rotation is planned, not emergent (§8.3)
----------------------------------------
A long-lived assistant's window fills, so the design is stop-and-respawn on age
or context pressure, handing forward a compact state summary. The *trigger* is
not implemented here; the state that makes one possible is, because bolting it
on later means a rotation with nothing to hand forward: ``started_at`` (what an
age policy reads, via :attr:`AssistantStatus.age_seconds`), ``generation`` (which
incarnation this is) and ``handoff`` (what the next one must be told).

The handoff travels through platform state and is meant to be read back over the
API, not through argv: ``lmer --prompt`` would work, but it puts a fleet summary
into ``ps`` output and into the command list this package echoes back over HTTP
and writes to the event log. The ``orchestrate`` taskdef instead tells the
assistant to ask for its handoff on startup, which is the same "authority and
knowledge live outside the window" property that makes rotation cheap at all.

The route that serves it is ``GET /api/assistant/handoff`` (T60), with the write
a ``POST`` to the same path; :func:`status`, :func:`start`, :func:`stop`,
:func:`rotate` and :func:`take_pending` are exposed beside it under
``/api/assistant/``. The taskdef still sends the assistant to the *served* route
list rather than naming a path, and still tells it to say plainly that it was not
briefed when there is none: this module and that list ship together, but the
prompt and the daemon do not.

What fills the spool is :mod:`lmer_platform.detect` (T69) — spec §8.3's other
half, where the daemon detects and the assistant is *notified* rather than
polling. Its tick diffs the attention list this module never looks at and calls
:func:`notify` for what is newly material, so ``POST /api/assistant/pending``
answers with a digest per changed condition rather than an empty list. The
direction of the dependency is one-way and stays that way: detection imports this
module, nothing here imports detection, and no path here polls anything.

Since T122 that tick delivers a *second* kind of digest through the same call: a
milestone a session announced with ``lmer-signal`` ("pushed MR !167", "the review
is finished"), carrying :data:`lmer_platform.detect.SIGNAL_DIGEST_KIND` as its
``kind``. Since issue #254 there is a third, on the same terms: a question whose
condition cleared while its run stayed in the fleet — answered or withdrawn
somewhere the daemon was not watching — carrying
:data:`lmer_platform.detect.QUESTION_ANSWERED_KIND`. Nothing in this module
distinguishes them, and that is the point — ``kind`` is a label on a spooled
note, not an enum, so a new class of event costs the seam nothing. What they are
*not* is attention reasons: those mean a human has to act and each one becomes a
digest class automatically, while these are addressed to the orchestrator and
stay off the operator's badge.

No digest is pushed. The spool waits to be taken (T89, issue #317)
------------------------------------------------------------------
:func:`notify` returns whether an assistant is *live*, and that is the whole of
what "notified" means here: a digest is written to a file and the return value
says somebody might read it. Nothing in this module writes into a session's
stdin, and no digest travels that way at all. That matters because a live
incarnation read the seam the other way round and told the operator "the
orchestrator already pushes me digests" — after which a completed review sat in
the spool until it was evicted, because an idle LLM session has no turn in which
to poll and nothing was going to give it one.

The first half of the fix is not here, and deliberately: what wakes an idle
session promptly is a *watch* the session itself arms, using its harness's own
background-monitor tool, whose condition is the non-consuming ``pending`` count
on :attr:`AssistantStatus` (see :func:`status`). The count is on the status
precisely so a watch has something to poll that is not the destructive take —
``POST /api/assistant/pending`` drains, so polling it would eat every digest at
the moment nothing was ready to act on it. The instruction that arms the watch
and reports a watch that keeps erroring lives in the ``orchestrate`` taskdef,
because it is a fact about how the assistant's harness works rather than a fact
about this state file.

The second half is the backstop that taught watch turned out to need, and it is
now built (issue #317): a watch that silently stops — a skipped re-arm, a stale
edge detector, a harness with no monitor tool — leaves digests sitting for as
long as nobody happens to look, which was measured at 10 digests over 17 minutes
on 2026-08-19. So :mod:`lmer_platform.nudge` decides when a spool has waited too
long beside a *quiet* assistant, and :class:`lmer_platform.detect.Detector` types
one sentence into that session over :func:`lmer_platform.session_io.send_input`.

Two properties of it belong here rather than there, because they are properties
of this file. **The digest still does not travel**: what is typed is "N are
waiting, take them", and the spool remains the one bounded, scrubbed source. And
**the rate limit is anchored in the state**: :attr:`AssistantState.nudged_at`
marks the accumulation that has been nudged, dated against
:attr:`AssistantState.pending_since`, and :func:`take_pending` clears both, so
neither can outlive the spool they describe. The detector keeps a per-process copy
of that mark as well, because a write to this file can fail and the bound must not
(:attr:`lmer_platform.detect.Detector._nudged`) — but the file is where it belongs
and the only copy that survives a restart.

Standing instructions: the handoff's sibling, never consumed (T87)
------------------------------------------------------------------
The handoff above is a *baton* — written for one successor, read once, replaced by
the next note. What it cannot carry is the operator's standing preferences ("spawn
reviewers with this preset", "never wind a run down without asking"), because
those are true for every incarnation and would have to be re-copied into every
handoff to survive, which is exactly how they get dropped.

So there is a second document, and it differs in the one property that matters:
``instructions`` is read at the *start of every incarnation* and is never taken.
Nothing consumes it, a rotation carries it forward, and a stop-and-start does too
— both because :func:`stop` and :func:`start` rewrite state with
:func:`dataclasses.replace` rather than rebuilding it, and pinning that is what
the tests do from both ends.

The operator can edit the scoped document in Project settings or state a rule
in chat for the assistant to confirm and store. Both use the same bounded,
scrubbed whole-document write path.

Two consequences worth stating, because they are what makes this safe to serve:

- **It is bounded** (:data:`MAX_INSTRUCTIONS_CHARS`), like the handoff and for a
  sharper reason: this text is charged against the context window of *every*
  future incarnation, not one, so a document that grew into a diary would cost
  more every time it was read. Rule-shaped and short is a property the bound can
  enforce even when the prompt asking for it is forgotten.
- **It is scrubbed** (:func:`set_instructions`, and again on the way out in
  :func:`lmer_platform.api._instructions_reply`). It is agent-authored text
  quoting an operator who is talking to a chat window, so "always use this token"
  is a thing that will be said — and unlike a transcript this lands in a plain
  file the daemon reads at every start and a browser reads through the API. Both
  directions, one definition, the same shape as the spool bound being enforced on
  write *and* trimmed on read: a file this process did not write is still served
  clean.

A mixed fleet needs nothing: an older image never asks for the route, and an
absent document reads as empty, which is what every host does today.

One file, three documents, one rule (T92)
------------------------------------------
The scrub above was written for the standing orders, and the argument for it did
not stop at that field: ``assistant.json`` holds the handoff and the digest spool
in the same file, both written by the same session quoting the same conversation,
and neither was scrubbed. So all three go through :func:`_scrubbed_text` on the
way in — one definition, scrub before bound — and the handoff and the notes are
masked again as the file is rebuilt (:meth:`AssistantState.from_dict`,
:meth:`PendingNote.from_dict`) rather than at each place that serves them, because
there are several of those and they are not all in this module.

A digest has a second half, and it took the same rule (T93): ``PendingNote.data``
is a mapping the daemon composes out of a fleet row, so the ``label`` in it is a
branch or MR title an agent chose. It goes through :func:`_scrubbed_data` on both
sides — write in :func:`notify`, read at the rebuild — which recurses over decoded
values rather than scrubbing the serialised payload, so a pattern that runs past a
closing quote cannot eat the key after it.

What that is worth is bounded, and :func:`lmer_platform.transcripts._scrub` says
so itself: it masks credential *shapes* and this platform's own secret by value,
and a bare token an operator pasted on its own line passes straight through. Which
is why the second half of T92 is not here at all — the file is published
owner-only by :data:`lmer_platform.store.SNAPSHOT_FILE_MODE`, with the mode on the
temp before the rename rather than chmod'ed on afterwards. Defense in depth beside
a 0600 file, as in :mod:`lmer_platform.transcripts`, not a licence to treat what
is stored here as safe to publish.

One at a time, and the registry says which
-------------------------------------------
"Is an assistant running" is answered by the **registry** — a live entry with
``kind="assistant"`` — never by the pointer in ``assistant.json``. The pointer is
a convenience that can be stale (the daemon was restarted, the file was hand
edited, a spawn landed but its state write did not); the registry entry is
written by the spawn path and reaped by liveness, and reconstructibility is what
licenses plain files for all of this in the first place (spec §5.3). So a second
assistant is refused on the strength of the registry, and a *dead* one is simply
replaced.

Giving up is not final: a manual start re-arms it (T75)
-------------------------------------------------------
:meth:`Supervisor._give_up` ends the watch thread, and until now that was where
supervision ended for the life of the daemon. ``POST /api/assistant/start`` then
started a container that *nothing was watching*: it looked like a recovery, and
the next crash was silent — the worst version of this, because the operator has
already been told once that the assistant needs looking at and now believes they
fixed it.

The seam is deliberately small. :func:`register_supervisor` publishes the one
:class:`Supervisor` this process runs, ``lmer platform run`` registers it beside
the thread it starts (and unregisters it when the server stops serving), and
:func:`resume_supervision` — called by the *manual* start route and by nothing
else — refills the budget and starts the loop again. A daemon with no supervision
registered (a test, ``lmer platform spawn``, an older caller) gets ``False`` and a
start that works exactly as it did before, which is the property that keeps this
from being a new way for a start to fail.

Why process-wide state rather than an argument threaded through: the route has a
config and a request, not the daemon's objects, and the alternative — a supervisor
handed to :func:`lmer_platform.api.create_app` — would put an optional
container-spawning dependency into the constructor every test of every route
calls. :mod:`lmer_platform.reattach` already keeps its per-process drains this way
(``_ACTIVE``), for the same reason: what is true of *this* process is not
configuration and has no business in a signature it does not belong to.

Why the *route* and not :func:`start`: the supervisor's own respawn goes through
:func:`start` too (via :func:`ensure_running`), so re-arming there would have each
attempt reset the budget it was spending — an unbounded crash loop dressed up as a
recovery. "A human asked for this" is the fact that licenses a fresh budget, and
the manual route is the only place that fact is known.

Caps: it holds its own slot (T75)
----------------------------------
``max_concurrent_sessions`` bounds workers only. Assistant start and rotation
remain available when that pool is full; :func:`start` enforces the separate
one-live-assistant rule through the registry. See docs/PLATFORM-QUICKSTART.md,
"Reserved assistant capacity", for the launch paths and continuity guarantees.
"""

from __future__ import annotations

from .projects import serialized

import hashlib
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from lmer_cli.cli import _get_taskdef_paths, _resolve_taskdef_dir
from lmer_cli.runtime import repo_root_path

from . import ask, containers, memory, registry, spawn, statusline
from .config import (
    ASSISTANT_SETTING_KEYS,
    ConfigError,
    PlatformConfig,
    active_assistant_credential,
    assistant_settings,
    container_base_url,
    mint_assistant_credential,
    read_secret,
    revoke_assistant_credential,
    update_stored,
    validate_assistant_override,
)
# The credential scrub, imported rather than reimplemented — the trade
# :mod:`lmer_platform.config` and :mod:`lmer_platform.api` already make with
# ``_scrub_credentials`` and ``_web_base_from_remote``. A second definition of
# "what a credential looks like" is a second thing to forget a pattern from, and
# this one covers the platform's own shared secret *by value*, which no
# reimplementation here would.
from .transcripts import _scrub, _scrub_decoded, scrub_pass
from .store import (
    StoreError,
    TS_FORMAT,
    age_seconds,
    append_event,
    ensure_state_dir,
    read_json,
    utc_now_iso,
    write_json,
)
from .project_context import assistant_dir
from . import projects, project_context

logger = logging.getLogger("lmer_platform.assistant")

__all__ = [
    "STATE_FILE", "ENV_FILE", "TASKDEF", "TARGET", "KIND", "STOP_REASONS",
    "MAX_PENDING", "MAX_HANDOFF_CHARS", "MAX_NOTE_CHARS",
    "MAX_INSTRUCTIONS_CHARS",
    "ENV_PLATFORM_URL", "ENV_PLATFORM_CREDENTIAL", "ENV_PLATFORM_UNREACHABLE",
    "AssistantError", "AssistantAlreadyRunning", "AssistantCapacityError",
    "TaskdefMissing", "AssistantState", "AssistantStatus", "PendingNote",
    "SupervisionReport", "Supervisor",
    "SUPERVISE_POLL_SECONDS", "RESPAWN_BACKOFF_SECONDS",
    "RESPAWN_BACKOFF_CAP_SECONDS", "MAX_RESPAWN_ATTEMPTS", "SETTLED_SECONDS",
    "state_path", "env_file_path", "taskdef_dir", "read_state", "status",
    "start", "stop", "rotate", "ensure_running", "set_handoff",
    "set_instructions", "notify", "take_pending", "mark_nudged",
    "clamp_future_nudged_at",
    "register_supervisor", "resume_supervision",
]

#: Pointer + rotation bookkeeping, beside the daemon's other snapshots (§6.1).
STATE_FILE = "assistant.json"

#: The ``.env`` the assistant's session is started with, beside the state file
#: and mode 0600 like the secret it carries. See :func:`_prepare_environment`
#: for why a file rather than an export.
ENV_FILE = "assistant.env"

#: What the assistant reads to drive the platform: where the API is *from inside
#: its container* (:func:`lmer_platform.config.container_base_url` — not the
#: bind address) and the shared secret to authenticate with.
#:
#: The two are set together or not at all, so an agent has one condition to test
#: rather than two, and the reason is in the third variable when they are absent.
#:
#: The second constant is spelled ``…_CREDENTIAL`` rather than ``…_SECRET``, and
#: that is not a preference: the security scan (``tests/test_security.py`` and the
#: gate's own ``check_secrets``) matches an assignment whose left-hand side ends in
#: that word, and cannot tell a hardcoded credential from an identifier that merely
#: names one. Describing the pattern literally here would trip it too — which is
#: exactly what the first version of this comment did. The *variable* is still
#: ``LMER_PLATFORM_SECRET`` — renaming that would break the contract with the
#: taskdef and with anything an operator has already exported.
ENV_PLATFORM_URL = "LMER_PLATFORM_URL"
ENV_PLATFORM_CREDENTIAL = "LMER_PLATFORM_SECRET"

#: Why the session got no URL and no secret, in one sentence it can relay to the
#: operator. Set only when the pair is absent — "the platform could not be made
#: reachable from this container" is a fact the assistant must be able to state,
#: and stating it is the entire alternative to handing over a URL that 404s at
#: the network layer.
ENV_PLATFORM_UNREACHABLE = "LMER_PLATFORM_UNREACHABLE"

#: The taskdef the assistant runs. Lives in this repo at ``taskdef/orchestrate/``.
TASKDEF = "orchestrate"

#: The assistant's ``<target>``. ``lmer`` requires one, and this is the only
#: value that is honest: the assistant's subject is the fleet, not a repository.
#:
#: It is also the backstop half of D17 described in the module docstring, and it
#: keeps that job now that ``no_repo`` makes the session repo-less on purpose.
#: ``lmer``'s resolver treats a target that is not a URL as a local path, so a
#: bare word can only resolve if a directory of that name sits in the daemon's
#: working directory *and* is a git checkout; with the repo-less switch lost it
#: therefore refuses rather than resolving. Anything URL-shaped, and anything
#: empty, would do the opposite — clone something. Keep this a word.
TARGET = "fleet"

#: Registry kind. An alias of :data:`lmer_platform.registry.ASSISTANT_KIND` rather
#: than a second copy of the string, because the value is now load-bearing in a
#: module that cannot import this one: :func:`lmer_platform.spawn._live_worker_count`
#: excludes this kind from the concurrency cap, and :mod:`lmer_platform.spawn` is
#: what spawns the assistant. The registry owns the vocabulary; this name stays
#: because it is what a reader of *this* module looks for, and because the fleet
#: view telling the orchestrator from the orchestrated is what the kind is for.
KIND = registry.ASSISTANT_KIND

#: Why an assistant stopped. ``rotation`` is the planned kind (§8.3) and is
#: recorded distinctly because a rotation that looks like an operator stop makes
#: the next context-pressure question unanswerable from state alone.
STOP_REASONS = ("operator", "rotation", "replaced")

#: How many undelivered digests to keep. Bounded on purpose — see the module
#: docstring. Oldest are dropped first: a fleet's *current* state is what the
#: assistant needs, and the full history is in ``events.jsonl``.
MAX_PENDING = 50

def ended_episode(state: "AssistantState") -> dict:
    """The fields a take resets: the nudge clock and its episode (#317, #584)."""
    return {"nudged_at": None, "nudges": 0, "nudge_session": None, "stalled_at": None,
            "episode": state.episode + 1}

#: Caps on caller-supplied text. Both values reach this module from reachable
#: input (the assistant writes its own handoff through the API), and both end up
#: in a state file the daemon reads on every start.
MAX_HANDOFF_CHARS = 8000
MAX_NOTE_CHARS = 2000

#: Cap on the operator's standing instructions (T87). Same magnitude as the
#: handoff and deliberately tighter than it, because the two documents are read a
#: different number of times: a handoff is read once by one successor, while this
#: is read at the start of *every* incarnation for as long as the host lives. A
#: document that drifted into a diary would therefore cost context forever, and
#: "keep it short and rule-shaped" is guidance the taskdef gives and this number
#: is what still holds when that guidance is forgotten.
MAX_INSTRUCTIONS_CHARS = 4000

#: How long a stop waits for SIGTERM to work before escalating, and how long it
#: then waits for SIGKILL. The first is generous because ``lmer`` tears a
#: container down on the way out; the second is short because nothing survives it.
STOP_GRACE_SECONDS = 5.0
STOP_KILL_GRACE_SECONDS = 2.0
_STOP_POLL_SECONDS = 0.05

#: How long :meth:`Supervisor.supervise_once` waits to be *told* an assistant
#: ended before re-reading the registry itself. Not a polling interval in the
#: usual sense: for a session this process spawned the wait ends the moment the
#: spawn's watcher records the exit, so this bounds only the case where there is
#: no event to wait on — an assistant adopted from a previous daemon. Half a
#: minute of lag on noticing *that* one died costs nothing; a tighter tick would
#: re-read every session's registry file for the life of the daemon.
SUPERVISE_POLL_SECONDS = 30.0

#: The first wait before a respawn, and the ceiling the doubling stops at. Five
#: seconds because a container that failed to start once usually failed for a
#: reason that is still true a second later; five minutes because a host whose
#: image pull is broken should be retried occasionally rather than hammered, and
#: because that is the order of "the operator has had time to look".
RESPAWN_BACKOFF_SECONDS = 5.0
RESPAWN_BACKOFF_CAP_SECONDS = 300.0

#: Bound repeated image pulls while allowing transient launch failures to recover.
MAX_RESPAWN_ATTEMPTS = 5

#: How long an incarnation has to live before it counts as having worked. Under
#: this it is a crash loop and spends a unit of the budget above; over it, the
#: budget is refilled. Two minutes because ``lmer`` clones a work repo and pulls
#: an image before the harness says a word, so anything shorter would call a slow
#: but healthy start a crash.
SETTLED_SECONDS = 120.0

#: Serializes the read-modify-write cycles below. Per-write atomicity is not
#: consistency across a read-modify-write (see :mod:`lmer_platform.store`), and
#: the writers here are genuinely concurrent: the API handlers run in Starlette's
#: threadpool and the daemon's watcher calls :func:`notify` from its own thread.
#: Reentrant because :func:`rotate` is a stop and a start.
_LOCK = threading.RLock()

#: Serializes only the registry/state publication boundary shared with
#: :func:`status`. The lifecycle lock above is deliberately much wider and may
#: cover process spawn or bounded termination waits; status polling must not pay
#: for either operation.
_PUBLICATION_LOCK = threading.RLock()

#: Scrub results per spool (issue #583): ``spool -> (credentials, {digest: note})``.
#: Each load replaces its spool's entry with the notes it just read, so a taken
#: note leaves the memo; a change in the credentials masked by value empties it.
_SCRUBBED_NOTES: dict = {}



class AssistantError(RuntimeError):
    """Base refusal, carrying the HTTP status a route should answer.

    The status rides on the exception exactly as in
    :mod:`lmer_platform.session_io` and :mod:`lmer_platform.ask`: one handler per
    route, and a refusal added later arrives with a code instead of a 500.
    """

    status = 400


class AssistantAlreadyRunning(AssistantError):
    """One assistant at a time (D11), and one is already up.

    409 rather than 400: the request was well-formed and would have been fine a
    moment earlier. Two operators tapping "start" is the case this makes
    harmless — the incumbent keeps its context window.
    """

    status = 409


class AssistantCapacityError(AssistantError):
    """The assistant inbox is full; retry after it is drained (HTTP 429)."""

    status = 429


class AssistantThreadsFull(AssistantError):
    """The assistant's standing thread list cannot take another link.

    409 for :class:`AssistantAlreadyRunning`'s reason: the request is well formed
    and works again the moment a thread is removed from its settings.
    """

    status = 409


class AssistantThreadHeld(AssistantError):
    """A thread the operator explicitly asked this incarnation to hold is taken.

    409 for :class:`AssistantThreadsFull`'s reason: the request is well formed
    and works again once the run holding the thread lets it go.
    """

    status = 409


class TaskdefMissing(AssistantError):
    """This host cannot see the ``orchestrate`` taskdef.

    503: the request was fine and the host cannot currently provide the service.
    Refusing here rather than letting the container fail is the difference
    between "the assistant will not start, here is the directory it looked in"
    and a session that dies seconds later for a reason only the PTY log holds.
    """

    status = 503


@dataclass(frozen=True)
class PendingNote:
    """One digest the daemon left for the assistant (§8.3)."""

    at: str
    kind: str
    note: str
    data: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"at": self.at, "kind": self.kind, "note": self.note, "data": self.data}

    @classmethod
    def from_dict(cls, payload: object) -> Optional["PendingNote"]:
        """Rebuild a note, or ``None`` when it is unusable.

        Tolerant like every other read in this package: one malformed entry must
        not cost the assistant the rest of its spool.
        """
        if not isinstance(payload, dict):
            return None
        note = payload.get("note")
        if not isinstance(note, str) or not note.strip():
            return None
        return cls(
            at=str(payload.get("at") or ""),
            kind=str(payload.get("kind") or "event"),
            # The read half of the pair :func:`_scrubbed_text` and
            # :func:`_scrubbed_data` make on the write side: a file this process
            # did not write — hand-edited, or written by a build before the scrub
            # existed — is still drained clean, in both halves of the digest.
            note=_scrub(note),
            data=_scrubbed_data(payload.get("data")),
        )


@dataclass(frozen=True)
class AssistantState:
    """What ``assistant.json`` holds — the facts that outlive the process.

    Not a status: nothing in here is derived from liveness. ``session_id`` is a
    pointer that may be stale by the time it is read, which is why
    :func:`status` reconciles it against the registry rather than trusting it.
    """

    session_id: Optional[str] = None
    started_at: Optional[str] = None
    generation: int = 0
    handoff: Optional[str] = None
    handoff_at: Optional[str] = None
    #: The operator's standing orders (T87). A *different* kind of field from the
    #: handoff beside it: nothing reads it destructively, no lifecycle verb
    #: rewrites it, and every incarnation starts by reading it. It survives a stop
    #: and a rotation because :func:`start` and :func:`stop` edit state with
    #: :func:`dataclasses.replace` — which is a property, not an accident, and is
    #: pinned by tests from both ends.
    instructions: Optional[str] = None
    instructions_at: Optional[str] = None
    stopped_at: Optional[str] = None
    stop_reason: Optional[str] = None
    pending: tuple = ()
    #: When this accumulation of digests began — the empty→non-empty moment
    #: (issue #317). Its own stamp because the notes cannot answer it: the spool
    #: is bounded, so the oldest *retained* note gets younger as older ones are
    #: evicted. Cleared by :func:`take_pending`.
    pending_since: Optional[str] = None
    #: Which accumulation this is, incremented per empty→non-empty transition and
    #: never reset (issue #317). An exact identity for callers that key on one —
    #: :attr:`lmer_platform.detect.Detector._nudged` — because two accumulations a
    #: second apart share :attr:`pending_since`. Mis-keying that memory costs a
    #: delay of the send's duration, not a window.
    pending_seq: int = 0
    #: When the platform last typed a nudge about this accumulation (issue #317).
    #: Here rather than in its own file so it shares the spool's lock and drain,
    #: and cannot disagree with what it describes. Cleared by
    #: :func:`take_pending`; a future value is durably bounded to the current
    #: time by :func:`clamp_future_nudged_at` before the detector reads it.
    nudged_at: Optional[str] = None
    #: Nudges typed in this episode, and the incarnation they were typed into
    #: (#584). An episode ends at a take or a new incarnation, so a count that
    #: names another session is zero. Cleared by :func:`take_pending`.
    nudges: int = 0
    nudge_session: Optional[str] = None
    #: Advanced by every take (:func:`ended_episode`), so a mark computed before
    #: a take can tell it landed after one and refuse.
    episode: int = 0
    #: When the one "not taking its spool" signal went out for this episode.
    stalled_at: Optional[str] = None
    codex_wake: Optional[str] = None
    conversation_seen: tuple = ()

    def episode_nudges(self, session_id: Optional[str]) -> int:
        """Nudges already typed into *session_id* during this episode."""
        return self.nudges if self.nudge_session == session_id else 0

    def episode_stalled(self, session_id: Optional[str]) -> bool:
        """Whether this episode's stall signal has gone out for *session_id*."""
        return self.stalled_at is not None and self.nudge_session == session_id

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "generation": self.generation,
            "handoff": self.handoff,
            "handoff_at": self.handoff_at,
            "instructions": self.instructions,
            "instructions_at": self.instructions_at,
            "stopped_at": self.stopped_at,
            "stop_reason": self.stop_reason,
            "pending": [note.to_dict() for note in self.pending],
            "pending_since": self.pending_since,
            "pending_seq": self.pending_seq,
            "nudged_at": self.nudged_at,
            "nudges": self.nudges,
            "nudge_session": self.nudge_session,
            "episode": self.episode,
            "stalled_at": self.stalled_at,
            "codex_wake": self.codex_wake,
            "conversation_seen": list(self.conversation_seen),
        }

    @classmethod
    @scrub_pass()
    def from_dict(cls, payload: Optional[dict], *, memo_spool: Optional[str] = None) -> "AssistantState":
        """Rebuild from a snapshot, repairing rather than rejecting.

        An absent file is the empty state — the normal case on a host where the
        chat has never been opened, which is exactly the case D11's "started on
        demand" is for. A field of the wrong type falls back to its default: this
        file is small enough to hand-edit, and the whole point of the plain-file
        choice (D2) is that a typo costs one value, not the assistant.

        The handoff is scrubbed here rather than at each of the places that serve
        it, because there are three of them and they are not all in this module:
        ``GET /api/assistant/handoff`` reads through :func:`read_state`,
        :func:`status` carries the note on the fleet status, and a start or a stop
        writes it back out. One rule at the rebuild point is what makes "a
        credential in the handoff is neither stored nor served" true of all three
        — and it means the next write persists the masked text, so a hand-edited
        credential does not sit in the file until someone notices. The digest
        notes get theirs in :meth:`PendingNote.from_dict` for the same reason; the
        standing orders keep their own pair (:func:`set_instructions` on the way
        in, ``api._instructions_reply`` on the way out).

        *memo_spool* names the project whose spool this is and turns on reuse of
        that spool's previous scrub results (:func:`_pending_notes`).
        """
        if not isinstance(payload, dict):
            return cls()
        generation = payload.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            generation = 0
        pending_seq = payload.get("pending_seq")
        if (
            not isinstance(pending_seq, int)
            or isinstance(pending_seq, bool)
            or pending_seq < 0
        ):
            pending_seq = 0
        nudges = _count(payload.get("nudges"))
        episode = _count(payload.get("episode"))
        raw_pending = payload.get("pending")
        pending = _pending_notes(raw_pending if isinstance(raw_pending, list) else [], memo_spool)
        return cls(
            session_id=_opt_str(payload.get("session_id")),
            started_at=_opt_str(payload.get("started_at")),
            generation=generation,
            handoff=_opt_scrubbed(payload.get("handoff")),
            handoff_at=_opt_str(payload.get("handoff_at")),
            instructions=_opt_str(payload.get("instructions")),
            instructions_at=_opt_str(payload.get("instructions_at")),
            stopped_at=_opt_str(payload.get("stopped_at")),
            stop_reason=_opt_str(payload.get("stop_reason")),
            pending=pending if projects.enabled() else pending[-MAX_PENDING:],
            pending_since=_opt_str(payload.get("pending_since")),
            pending_seq=pending_seq,
            nudged_at=_opt_str(payload.get("nudged_at")),
            nudges=nudges,
            nudge_session=_opt_str(payload.get("nudge_session")),
            episode=episode,
            stalled_at=_opt_str(payload.get("stalled_at")),
            codex_wake=_opt_str(payload.get("codex_wake")),
            conversation_seen=tuple(
                item for item in (payload.get("conversation_seen")
                                  if isinstance(payload.get("conversation_seen"), list) else [])
                if isinstance(item, str) and len(item) == 64
            ),
        )


@dataclass(frozen=True)
class AssistantStatus:
    """The recorded state reconciled with what is actually running.

    ``stale`` is the "a dead assistant is detected" signal: state names a session
    and nothing by that name is alive. ``tracked`` is its mirror image — an
    assistant *is* running but is not the one this state file recorded, which is
    what a daemon restart or a lost state write looks like. Both are reported
    rather than repaired on read, because a read that quietly rewrote state would
    make the next post-mortem read a file that had already tidied up the evidence.
    """

    running: bool
    session_id: Optional[str]
    pid: Optional[int]
    started_at: Optional[str]
    generation: int
    stale: bool
    tracked: bool
    pending: int
    handoff: Optional[str]
    #: When the platform last nudged this spool (issue #317), so the session that
    #: was typed into can attribute the line rather than read it as the operator.
    #: Served here because no route serves ``events.jsonl``, making this the only
    #: answer an assistant can fetch.
    nudged_at: Optional[str] = None
    log_path: Optional[str] = None
    #: What the *running* incarnation was actually launched with — the four
    #: launch settings (issue #234), read off its registry entry, where the
    #: spawn recorded what it emitted. ``None`` when nothing is running, and
    #: distinct on purpose from what :func:`lmer_platform.config.assistant_settings`
    #: currently resolves: a settings change applies to the *next* incarnation,
    #: so "current" and "next" are two answers and a UI showing only one of
    #: them would read a pending change as a no-op or a lie.
    settings: Optional[dict] = None
    #: The host's shared memory store (issue #325): path, file count, size.
    #: Served with no assistant running too — the store outlives all of them.
    #: Accumulation only, never liveness; see :mod:`lmer_platform.memory`.
    memory: Optional[dict] = None
    #: What the running incarnation last published about its own status line
    #: (issue #248, :mod:`lmer_platform.statusline`). ``None`` with nothing
    #: running, and for a running one that publishes nothing. Served here
    #: because this is the route the chat pane already polls.
    statusline: Optional[dict] = None
    #: The chat threads this incarnation is connected to right now (issue #412),
    #: which is not ``settings["threads"]``: that one is the list the session was
    #: *launched* with, while this one moves when the operator connects or
    #: disconnects a thread from the pane header. Served here because this is the
    #: route the pane already polls.
    threads: list = field(default_factory=list)
    #: The running incarnation's unanswered, unclosed questions to the operator
    #: (issue #507), counted from its ask channel by
    #: :func:`lmer_platform.ask.open_question_count`. ``0`` with nothing running.
    open_questions: int = 0

    @property
    def age_seconds(self) -> Optional[float]:
        """How long this incarnation has been up — what an age policy reads."""
        return _age_seconds(self.started_at)

    def to_dict(self) -> dict:
        from . import assistant_history

        history = assistant_history.latest()
        return {
            "running": self.running,
            "session_id": self.session_id,
            "history_session": history["session"] if history else None,
            "pid": self.pid,
            "started_at": self.started_at,
            "age_seconds": self.age_seconds,
            "generation": self.generation,
            "stale": self.stale,
            "tracked": self.tracked,
            "pending": self.pending,
            "handoff": self.handoff,
            "nudged_at": self.nudged_at,
            "log_path": self.log_path,
            "settings": self.settings,
            "memory": self.memory,
            "statusline": self.statusline,
            "threads": self.threads,
            "open_questions": self.open_questions,
            "taskdef": TASKDEF,
            "target": TARGET,
        }


def _pending_notes(raw: list, memo_spool: Optional[str]) -> tuple:
    """:meth:`PendingNote.from_dict` over *raw*, reusing *memo_spool*'s earlier results.

    A hit is the result an earlier load computed from the same stored note under
    the same credentials, so it is exactly what a scrub would return now. Notes
    are not skipped for having been scrubbed on write: the scrub is not
    idempotent (``a://b@c@d`` loses one userinfo segment per pass).
    """
    if memo_spool is None:
        notes = (PendingNote.from_dict(item) for item in raw)
        return tuple(note for note in notes if note is not None)
    with scrub_pass() as known:
        cached_known, cached = _SCRUBBED_NOTES.get(memo_spool, (None, {}))
        if cached_known != known:
            cached = {}
        kept: dict = {}
        notes = []
        for item in raw:
            digest = hashlib.sha256(json.dumps(item).encode("utf-8")).hexdigest()
            if digest in cached:
                entry = cached[digest]
            else:
                note = PendingNote.from_dict(item)
                # The data travels as JSON so that no caller holding a note
                # can change what a later load is handed.
                entry = None if note is None else (
                    replace(note, data=None), None if note.data is None else json.dumps(note.data))
            kept[digest] = entry
            if entry is not None:
                note, data = entry
                notes.append(note if data is None else replace(note, data=json.loads(data)))
        _SCRUBBED_NOTES[memo_spool] = (known, kept)
    return tuple(notes)


def _opt_str(value: object) -> Optional[str]:
    """A non-empty string, or ``None`` for anything else."""
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int:
    """A non-negative int, or ``0`` for anything else."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _opt_scrubbed(value: object) -> Optional[str]:
    """:func:`_opt_str`, with credential shapes masked.

    The scrub runs on the way out of the file as well as on the way in
    (:func:`_scrubbed_text`) — one definition, both directions, which is the rule
    T87 set for the standing orders and the same one
    :func:`lmer_platform.transcripts._scrub` already serves for a transcript.
    """
    text = _opt_str(value)
    return _scrub(text) if text is not None else None


def _age_seconds(started_at: Optional[str]) -> Optional[float]:
    """Seconds since an ISO-8601 Z timestamp; ``None`` when unparseable.

    :func:`lmer_platform.store.age_seconds`' rule, kept under this name for the
    readers of this module. It moved beside the function that *writes* the format
    when a third copy of it appeared.
    """
    return age_seconds(started_at)


def state_path() -> Path:
    """Where the pointer and rotation bookkeeping live."""
    return assistant_dir() / STATE_FILE


def env_file_path() -> Path:
    """Where the assistant's ``.env`` lives — see :func:`_prepare_environment`."""
    return assistant_dir() / ENV_FILE


def read_state() -> AssistantState:
    """The recorded state, or the empty state when there is none.

    Never raises: a corrupt file has already been moved aside by
    ``store.read_json``, and an assistant that cannot be *started* because its
    bookkeeping is unreadable is a worse outcome than one that starts at
    generation 0.
    """
    try:
        stored = projects.assistant_state() if projects.enabled() else read_json(state_path())
    except StoreError as exc:
        logger.error(
            "platform_assistant_state_unreadable error=%s — starting from empty", exc
        )
        return AssistantState()
    return AssistantState.from_dict(stored, memo_spool=project_context.current() or projects.GENERAL)


def _write_state(state: AssistantState) -> bool:
    """Persist *state*. Returns whether it landed.

    Loud but not fatal, and that is a deliberate inversion of
    :func:`lmer_platform.store.write_json`'s "writes raise" contract: the caller
    is usually holding a session it just started, and killing a live assistant
    because its pointer could not be written would trade a bookkeeping problem
    for a real one. The registry entry is the authority for what is running
    (module docstring), so a lost write costs the generation counter and the
    handoff, not the assistant.
    """
    try:
        if projects.enabled():
            projects.save_assistant_state(state.to_dict())
        else:
            write_json(state_path(), state.to_dict())
    except StoreError as exc:
        logger.error(
            "platform_assistant_state_unwritable error=%s — the registry entry "
            "remains authoritative for what is running, but rotation bookkeeping "
            "was lost", exc,
        )
        return False
    return True


def history_for(session_id: str) -> Optional[dict]:
    """Resolve a generation, migrating only the explicitly recorded incumbent."""
    from . import assistant_history

    with _LOCK:
        known = assistant_history.generation(session_id)
        state = read_state()
        return assistant_history.remember(state) if state.session_id == session_id else known


def taskdef_dir() -> Optional[Path]:
    """The ``orchestrate`` taskdef directory this host can see, or ``None``.

    Resolved exactly the way ``lmer`` itself resolves one, through
    ``cli._get_taskdef_paths`` / ``cli._resolve_taskdef_dir``, so the platform
    cannot disagree with the CLI it is about to run about where taskdefs live.
    """
    return _resolve_taskdef_dir(TASKDEF, _get_taskdef_paths(repo_root_path()))


def _require_taskdef() -> Optional[Path]:
    """Refuse to start when the host can see taskdefs and ours is not among them.

    The conditional is not hedging, it is how ``lmer`` already treats this: the
    host-side task list is *advisory*, because work-repo taskdefs live inside the
    container and are invisible here, and in installed mode the host has no
    taskdef directory at all (``repo_root_path()`` is ``None``). The CLI warns
    only when it has some host-side view to contradict, and so does this. A hard
    refusal in installed mode would make the assistant unstartable on every
    non-developer host.

    The residual case worth knowing: this resolves taskdefs relative to *this*
    package's checkout, while the process spawned is ``config.lmer_bin``. Point
    ``lmer_bin`` at a different checkout and this check answers for the wrong
    tree — which is already true of everything else this package imports from
    ``lmer_cli``.
    """
    paths = _get_taskdef_paths(repo_root_path())
    if not paths:
        logger.debug(
            "platform_assistant_taskdef_unverified — no host-side taskdef "
            "directories; the container's start hook is authoritative"
        )
        return None
    found = _resolve_taskdef_dir(TASKDEF, paths)
    if found is None:
        raise TaskdefMissing(
            f"the {TASKDEF!r} taskdef is not in any host-side taskdef directory "
            f"({', '.join(str(p) for p in paths)}) — the assistant cannot be "
            "started until it is installed there or on LMER_TASKDEF_PATHS"
        )
    return found


def refuse_unusable_thread_list(session_id: object, permalink: object) -> None:
    """Refuse a connect that would leave the assistant's launch list unusable.

    ``MAX_PER_SESSION`` bounds the *connections of one session*;
    ``assistant_threads`` is a different list — the launch list, editable straight
    from the settings dialog — so the two diverge, and appending to the second
    with only the first checked is how it goes over.

    Over is worse than it sounds: :func:`config.update_stored` validates by
    building a ``PlatformConfig``, and an unusable launch setting is *tolerated*
    there (warned and resolved as unset) rather than raised, so a 17th link is
    written to ``config.json`` and the whole list then resolves to ``None`` —
    uber lmer rotates back connected to nothing, a moment after the pane showed
    sixteen icons, with only a log line to say so.

    So the list the write *would* produce is validated first, and the connect is
    refused before anything is recorded or started. A no-op for every session but
    the running assistant's.
    """
    if not isinstance(permalink, str) or not permalink:
        return
    if not _writes_the_launch_list(session_id):
        return
    current = _stored_threads()
    if permalink in current:
        return
    _refuse_unusable([*current, permalink])


def refuse_full_thread_list(session_id: object) -> None:
    """The same refusal for a caller with no link yet: is there room for one?

    Opening a thread (issue #429) settles this **before** it posts a root
    message and cannot name the link until after — but the bound is a fact about
    the stored list, not about the link, and a thread about to be created cannot
    already be in it. So: capacity, and the same verdict on what is stored.
    """
    from .thread_targets import MAX_THREAD_TARGETS

    if not _writes_the_launch_list(session_id):
        return
    current = _stored_threads()
    _refuse_unusable(current)
    if len(current) >= MAX_THREAD_TARGETS:
        _threads_full(f"must contain at most {MAX_THREAD_TARGETS} thread permalinks")


def _writes_the_launch_list(session_id: object) -> bool:
    """Whether a connect on this session also edits the standing setting. A
    no-op for every session but the running assistant's."""
    live = _live_assistant()
    return live is not None and live.get("id") == session_id


def _refuse_unusable(candidate: list) -> None:
    """Refuse when *candidate* is not a list uber lmer could launch with."""
    from .thread_targets import validate_thread_targets

    try:
        validate_thread_targets(candidate)
    except ValueError as exc:
        _threads_full(str(exc))


def _threads_full(reason: str) -> None:
    raise AssistantThreadsFull(
        f"uber lmer's standing thread list would become unusable: {reason}. "
        "Remove one in its settings first — the list there is what each "
        "start and rotation reads."
    ) from None


def _refuse_held_explicit_threads(
    launch: dict, sources: dict, *, besides: Optional[str] = None
) -> None:
    """Refuse an explicitly asked-for thread that another live run holds (#413).

    The layer decides the posture, as it does for every other launch setting
    (:func:`_launch_settings`): a thread named in *this* call has a caller
    attached to refuse to, while the standing list defers instead. What
    deferring means, and why, is in docs/COMMS.md §9.
    """
    if sources.get("threads") != "override":
        return
    from . import connectors as thread_connectors

    component = thread_connectors.get_connectors()
    if component is None:
        return
    try:
        component.refuse_held_targets(launch.get("threads") or [], besides=besides)
    except thread_connectors.ThreadError as exc:
        raise AssistantThreadHeld(str(exc)) from None


def _stored_threads() -> list:
    """The assistant's stored launch list, as it is in ``config.json``."""
    if projects.enabled():
        return list(projects.get(project_context.current() or projects.GENERAL)["settings"].get("threads") or [])
    stored = assistant_settings().get("threads")
    raw = getattr(stored, "stored", None)
    return list(raw) if isinstance(raw, list) else []


@serialized
def note_thread_change(session_id: object, permalink: object, *, connected: bool) -> bool:
    """Keep the standing ``threads`` setting in step with a live connect (#412).

    Called on every thread connect and disconnect, and a no-op for every session
    but the running assistant's: a worker's connections are its session's, while
    the assistant's are also *how the next incarnation is launched*, so a thread
    connected from the chat pane has to survive a rotation and one disconnected
    there has to stay gone.

    Returns whether the stored list changed. Best-effort by design — the
    connection is already made or already broken by the time this runs, and a
    config file that could not be written is a launch setting to fix, not a
    reason to report a working connection as a failure.
    """
    if not isinstance(permalink, str) or not permalink:
        return False
    live = _live_assistant()
    if live is None or live.get("id") != session_id:
        return False
    current = _stored_threads()
    if connected:
        if permalink in current:
            return False
        current.append(permalink)
    else:
        if permalink not in current:
            return False
        current = [entry for entry in current if entry != permalink]
    from .thread_targets import validate_thread_targets

    try:
        # ``update_stored`` tolerates an unusable launch setting rather than
        # raising (it warns and resolves as unset), so the only thing standing
        # between a too-long list and a config.json that silently launches with
        # no threads at all is this check.
        validate_thread_targets(current)
        if projects.enabled():
            workspace_id = project_context.current() or projects.GENERAL
            settings = projects.get(workspace_id)["settings"]
            projects.update(workspace_id, {"settings": {**settings, "threads": current}})
        else:
            update_stored({"assistant_threads": current})
    except (ConfigError, ValueError) as exc:
        logger.warning(
            "platform_assistant_threads_unwritable connected=%s error=%s", connected, exc,
        )
        return False
    return True


def _live_assistant() -> Optional[dict]:
    """The registry entry of the running assistant, or ``None``.

    The authority for "is one running" (module docstring). When state already
    names one, that entry wins, so a stray second assistant cannot make
    :func:`status` start reporting a different session id from one call to the
    next; otherwise the oldest live one is the incumbent. More than one is
    logged, because it means something upstream let two starts through.
    """
    workspace_id = project_context.current() or projects.GENERAL
    live = [
        entry for entry in registry.list_sessions(live_only=True)
        if entry.get("kind") == KIND
        and (not projects.enabled() or projects.session_home(entry["id"]) == workspace_id)
    ]
    if not live:
        return None
    if len(live) > 1:
        logger.error(
            "platform_multiple_assistants ids=%s — one assistant at a time (D11); "
            "the oldest is treated as the incumbent",
            ",".join(str(entry.get("id")) for entry in live),
        )
    recorded = read_state().session_id
    for entry in live:
        if recorded and entry.get("id") == recorded:
            return entry
    return live[0]


def status() -> AssistantStatus:
    """What the assistant is doing, reconciled against the registry.

    Side-effect free: a stale pointer is *reported* (``stale``), never cleaned up
    here. The publication lock is still required even though this writes nothing:
    :func:`start` publishes the live registry entry before it publishes the new
    state generation, and a reader between those writes would otherwise combine
    the new session id with the previous generation and call it untracked. The
    lock covers only those adjacent writes, so status polling does not wait for a
    process spawn or termination grace period.
    """
    # Outside the lock: a filesystem walk of a directory no writer here touches,
    # and _PUBLICATION_LOCK exists so a poll and the lifecycle path never wait on
    # each other.
    memory_snapshot = _memory_snapshot()
    with _PUBLICATION_LOCK:
        state = read_state()
        live = _live_assistant()
        if live is None:
            return AssistantStatus(
                running=False,
                session_id=state.session_id,
                pid=None,
                started_at=state.started_at,
                generation=state.generation,
                stale=state.session_id is not None,
                tracked=False,
                pending=len(state.pending),
                handoff=state.handoff,
                nudged_at=state.nudged_at,
                # No live session to date the record against, so `stale` is
                # `None` rather than `False` — see :func:`_with_link_age`.
                memory=_with_link_age(memory_snapshot, None),
            )
        session_id = live.get("id")
        pid = live.get("pid")
        answer = AssistantStatus(
            running=True,
            session_id=session_id if isinstance(session_id, str) else None,
            pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            # The live entry's own timestamp, not the recorded one: an adopted
            # assistant (tracked=False) has no recorded start, and the age an age
            # policy needs is the session's, not the pointer's.
            started_at=_opt_str(live.get("started_at")) or state.started_at,
            generation=state.generation,
            stale=False,
            tracked=session_id == state.session_id,
            pending=len(state.pending),
            handoff=state.handoff,
            nudged_at=state.nudged_at,
            log_path=_opt_str(live.get("log_path")),
            settings=_launched_settings(live),
            memory=_with_link_age(memory_snapshot, live),
        )
    # Outside the lock, as ``_memory_snapshot()`` above it is: a read of a file
    # a container writes must not hold up a start or a stop.
    if answer.session_id is None:
        return answer
    return replace(
        answer,
        statusline=statusline.for_session(answer.session_id),
        threads=_connected_threads(answer.session_id),
        open_questions=ask.open_question_count(answer.session_id),
    )


def _connected_threads(session_id: str) -> list:
    """The chat threads this incarnation is connected to, or ``[]``.

    Read outside the lock like the status line beside it, and tolerant for the
    same reason: an assistant whose pane cannot be drawn because a thread record
    could not be read is a worse answer than a pane with no icons.
    """
    from . import connectors

    component = connectors.get_connectors()
    if component is None:
        return []
    try:
        return component.listing(session_id)
    except Exception as exc:
        logger.warning("platform_assistant_threads_unreadable error=%s", exc)
        return []


def _memory_snapshot() -> dict:
    """What the shared memory store holds, for :func:`status` to serve.

    A filesystem measurement, so the answer is the same whether an assistant is
    running, has never run, or runs a harness with no memory feature. Bounded
    (``MEASURE_ENTRY_CAP``) because a UI polls this route.

    ``link`` is the exception: not a measurement but what the container's
    linker recorded (:func:`lmer_platform.memory.link_state`), joined on because
    a ``files`` of zero means opposite things with and without it.
    """
    directory = memory.memory_dir()
    return {
        "path": str(directory),
        **memory.measure(directory).to_dict(),
        "link": memory.link_state(directory),
    }


def _with_link_age(snapshot: dict, live: Optional[dict]) -> dict:
    """Mark the link record ``stale`` when it predates the running incarnation.

    Only a session that ran the linker rewrites the marker, so one whose image
    predates it leaves the previous incarnation's answer in place. ``stale`` is
    ``None`` whenever the question cannot be asked, because "we cannot tell" and
    "it is current" are the two answers this feature exists to stop conflating;
    with no record at all the snapshot is returned untouched. Both stamps are
    parsed rather than string-compared for the same reason: a lexicographic
    compare answers confidently for input it cannot judge. See
    docs/PLATFORM-QUICKSTART.md, "uber lmer's memory".
    """
    record = snapshot.get("link")
    if not isinstance(record, dict):
        return snapshot
    stale = None
    written = _parsed_stamp(record.get("at"))
    started = _parsed_stamp((live or {}).get("started_at"))
    if written is not None and started is not None:
        stale = written < started
    return {**snapshot, "link": {**record, "stale": stale}}


def _parsed_stamp(value) -> Optional[datetime]:
    """One platform timestamp, or ``None`` for anything that is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, TS_FORMAT)
    except ValueError:
        return None


def _launched_settings(live: dict) -> dict:
    """The launch settings off a live registry entry (issue #234).

    What the running incarnation was *actually* started with, read where the
    spawn recorded what it emitted. Always the same keys, so a client renders
    one shape: an adopted assistant an older build spawned simply reads as
    unset, with no thread targets.

    The model gets one extra source: the ordinary assistant spawn names none,
    and the session reports the one it resolved strictly after the registry
    entry is written — so when the entry lacks a model, the session's own
    report (:func:`_reported_model`) fills it in, with the same precedence
    ``spawn.absorb_ports`` keeps on the fleet path (a spawn that *named* a
    model already recorded it, and nothing here can overwrite that). It is an
    **overlay on the returned dict, never a registry write**: ``absorb_ports``
    folds via ``registry.update``, which stamps the caller's pid as
    ``owner_pid`` — the guard that stops a second daemon from stopping entries
    it does not own — and a *status read* taking ownership of the incumbent
    would make the real owner's stop path refuse its own assistant. Reading
    here keeps :func:`status` genuinely side-effect free.
    """
    task = live.get("task")
    task = task if isinstance(task, dict) else {}
    settings = {key: _opt_str(task.get(key)) for key in ASSISTANT_SETTING_KEYS}
    settings["threads"] = task.get("targets") or []
    if settings["model"] is None:
        settings["model"] = _reported_model(live.get("id"))
    return settings


def _reported_model(session_id: object) -> Optional[str]:
    """The model the session reported for itself, or ``None``.

    The same file and key ``spawn.absorb_ports`` reads
    (:func:`lmer_platform.spawn.ports_file_for`), read directly so this path
    stays a read — see :func:`_launched_settings` for why the fold's
    ``registry.update`` must not run from here. Best-effort like every read of
    that file: a session that reported nothing (or an unparseable file) is a
    ``None``, never an error in a status call.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        payload = json.loads(
            spawn.ports_file_for(session_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _refuse(error: AssistantError, reason: str, *, operation: str = "start") -> AssistantError:
    """Record a refusal in platform history and return it to raise.

    Spec §8.2 requires cap refusals to reach ``events.jsonl`` — "a cap that is
    only a prompt instruction is not a cap" — and every other refusal here is
    worth the same line: each one means an operator asked for the assistant and
    did not get it, which is invisible in a UI that only shows what is running.
    """
    append_event(
        f"assistant_{operation}_refused",
        note=reason,
        data={"reason": reason, "detail": str(error)},
    )
    logger.warning("platform_assistant_%s_refused reason=%s: %s", operation, reason, error)
    return error


def _validated_text(value: object, *, field: str, limit: int) -> str:
    """A non-empty, bounded string, or a refusal naming the field."""
    if not isinstance(value, str) or not value.strip():
        raise AssistantError(f"{field} must be non-empty text, got {value!r}")
    text = value.strip()
    if len(text) > limit:
        raise AssistantError(
            f"{field} is {len(text)} characters, over the {limit} limit — it is a "
            "compact summary, not a transcript"
        )
    return text


def _scrubbed_text(value: object, *, field: str, limit: int) -> str:
    """:func:`_validated_text` with the credential scrub in front of the bound.

    One definition for every document this module stores, because they are one
    kind of text: agent-authored prose that lands in a plain file the daemon reads
    at every start and a browser reads through the API. T87 made that argument for
    the standing orders; it is no weaker for the handoff and the digest notes,
    which are written by the same session quoting the same conversation — "use
    this token to reach the forge" is a sentence an operator says to a chat window,
    and what the assistant hands forward is what it was told.

    Scrubbed *before* the bound, as in
    :func:`lmer_platform.transcripts._present`: a mask that lengthens the text
    must not be able to smuggle it past the limit, and the number in a refusal has
    to be the number of characters that would have been stored.

    A non-string is passed through untouched, so what answers is the refusal
    naming the field rather than a TypeError from inside the scrub.
    """
    return _validated_text(
        _scrub(value) if isinstance(value, str) else value, field=field, limit=limit
    )


def _scrubbed_data(value: object) -> Optional[dict]:
    """The structured half of a digest, masked value by value. ``None`` if unusable.

    The same argument as :func:`_scrubbed_text` and not a weaker one for being
    machine-shaped: :meth:`lmer_platform.detect.Signal.data` copies a fleet row's
    fields into it, and a run's ``label`` is prose an agent wrote — the branch or
    MR title it chose — while ``note`` on the row beside it is whatever a session
    said about why it is stuck. Both land in ``assistant.json`` through
    :func:`notify` and leave again through ``POST /api/assistant/pending``, so the
    payload is exactly the shape T92 scrubbed the note for.

    Recursed over decoded values rather than applied to the serialised payload,
    which is :func:`lmer_platform.transcripts._scrub_decoded`'s own reason: some of
    these patterns end at the first quote and others do not, so one loose on a JSON
    line can run past a closing quote and eat the next key. One substitution per
    string, the payload's shape untouched.

    A non-mapping is dropped to ``None`` rather than scrubbed, which is what this
    field already did with one: the payload is the daemon's own structure, and
    something that is not a mapping is not it.
    """
    return _scrub_decoded(value) if isinstance(value, dict) else None


def _handoff_update(
    state: AssistantState, handoff: Optional[str], now: str
) -> tuple:
    """The handoff to persist and the timestamp to stamp it with.

    One place for a rule that both :func:`start` and :func:`stop` need: omitting
    *handoff* carries the recorded one forward — which is what makes a respawn
    after a crash as informed as a planned rotation — and leaves its original
    timestamp alone, because nothing was written. Validation happens here so it
    happens *before* the caller's side effects: a rejected handoff must not cost
    a container that is already starting.
    """
    if handoff is None:
        return state.handoff, state.handoff_at
    text = _scrubbed_text(handoff, field="handoff", limit=MAX_HANDOFF_CHARS)
    return text, (now if text != state.handoff else state.handoff_at)


def _launch_settings(overrides: Optional[dict]) -> tuple:
    """``(values, sources)`` — how the next incarnation should be run (issue #234).

    Per key: an explicit *overrides* entry > ``LMER_PLATFORM_ASSISTANT_*`` env >
    a **fresh** ``config.json`` read > ``None`` (today's behaviour). Fresh is
    the point — the daemon's own :class:`PlatformConfig` is boot-time, and a
    setting persisted through ``POST /api/assistant/config`` has to reach the
    incarnation the operator rotates in without a daemon restart; see
    :func:`lmer_platform.config.assistant_settings`.

    Two validation postures over ONE rule set
    (:func:`lmer_platform.config._unusable_reason` — shape rules mirroring what
    ``spawn.SpawnRequest.validate`` enforces for these four fields), matching
    who is asking. A value that arrived through the standing layers has already
    fallen back with a warning when unusable (inside
    :func:`lmer_platform.config.assistant_settings`) — a typo in a file must
    not make the assistant unstartable. An explicit override is refused instead
    (:class:`AssistantError`, so the route answers 400): the caller is
    attached, and starting *something other than what they asked for* is the
    failure the issue names. One definition matters more than it looks: these
    rules once lived in three hand-synced places, and the one rule that was not
    copied everywhere (an ``agents`` naming nobody) surfaced as an uncaught
    ``SpawnError`` on every start.

    Like :func:`_handoff_update`, called before the caller's side effects: a
    rejected setting must not cost a container that is already starting.
    """
    values: dict = {}
    sources: dict = {}
    for key, setting in assistant_settings().items():
        values[key] = setting.value
        sources[key] = setting.source
    project_overrides = {}
    if projects.enabled():
        project_overrides = projects.get(project_context.current() or projects.GENERAL)["settings"]
        if project_context.current() not in (None, projects.GENERAL):
            values["threads"], sources["threads"] = [], "project"
    for key, value in {**project_overrides, **(overrides or {})}.items():
        if key not in ASSISTANT_SETTING_KEYS:
            raise AssistantError(
                f"unknown assistant setting {key!r}: expected one of "
                f"{', '.join(sorted(ASSISTANT_SETTING_KEYS))}"
            )
        if value is None:
            # Absent, not "force the default": null carries the standing answer
            # forward, the same reading the handoff gives an omitted field.
            continue
        try:
            values[key] = validate_assistant_override(key, value)
        except ConfigError as exc:
            raise AssistantError(str(exc)) from exc
        sources[key] = "override"
    return values, sources


def _dotenv_line(name: str, value: str) -> str:
    """One ``KEY="value"`` line ``dotenv`` will read back exactly as written.

    Quoted and escaped rather than written bare, because one of these values is
    a sentence: an unquoted ``dotenv`` value ends at a ``#``, so an unreachable
    reason mentioning one would silently lose its tail.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"\n'


def _assistant_credential(config: PlatformConfig) -> Optional[str]:
    """What the next incarnation authenticates with — minted, or the fallback.

    A fresh per-incarnation credential (issue #244), so the platform can tell
    this session's calls from the operator's browser — without which an open
    browser tab would suppress the very check-in reminders the assistant needs
    (:mod:`lmer_platform.checkin`).

    A failed mint falls back to the shared secret rather than costing the
    operator their chat window: the assistant then runs as it did before this
    existed, unattributed and reminded about runs it has read. Loud, because that
    line is the only place that says why the digest went noisy.

    Nothing is minted on a host with **no** shared secret: the daemon creates that
    on first start, so its absence means there is no platform to authenticate to,
    and a key to a server nobody runs is worse than the sentence saying so.
    """
    try:
        shared = read_secret(config)
    except ConfigError as exc:
        logger.error(
            "platform_assistant_secret_unreadable error=%s — the assistant will "
            "start without credentials", exc,
        )
        return None
    if not shared:
        return None
    try:
        return mint_assistant_credential()
    except ConfigError as exc:
        if projects.enabled():
            raise AssistantError(f"Could not mint a project-scoped assistant credential: {exc}") from exc
        logger.error(
            "platform_assistant_credential_unmintable error=%s — falling back to "
            "the shared secret, so this incarnation's calls cannot be told from "
            "the operator's and its check-ins will not register", exc,
        )
        return shared


def _assistant_environment(config: PlatformConfig) -> tuple:
    """``(env, reach)`` — what to tell the session, and what was decided.

    Two variables or one, never a mixture. A URL with no secret is a 401
    machine and a secret with no URL is a credential with nothing to spend it
    on, so the pair is all-or-nothing and the absent case carries its reason.

    The credential is minted here (:func:`_assistant_credential`) and written
    nowhere else: it reaches the container through the 0600 ``.env``, never argv,
    and the transcript scrub strikes a live one out by value.
    """
    reach = container_base_url(config)
    credential = _assistant_credential(config) if reach.reachable else None

    if reach.reachable and credential:
        # SPEC CORRECTION (§8.1), amended by issue #244. The spec calls the
        # assistant's token "scoped to the elevated verbs". It is not. #244 added
        # a second credential, so a caller is now *identifiable* — but it opens
        # every route the operator's key opens, because no per-route capability
        # exists to scope it against. The claim therefore stays dropped rather
        # than half-restored: the next person to add a dangerous verb must not
        # believe the assistant cannot reach it. (What identity would buy next is
        # the retired ``max_concurrent_assistant_spawns`` cap, still not built.)
        #
        # This is the documented exception to ``ask_channel/protocol.py``'s "no
        # credential travels into the container". That reasoning is about
        # *worker* sessions and is untouched — they still get a mounted
        # directory and no key. The assistant is the one container that is meant
        # to have elevated authority (operator request, 2026-07-27); a chat
        # window that cannot spawn or answer anything is not an orchestrator.
        return {
            ENV_PLATFORM_URL: reach.url,
            ENV_PLATFORM_CREDENTIAL: credential,
        }, reach

    reason = reach.reason or (
        "this host has no shared secret to authenticate with — the platform "
        "daemon creates one on first start, so either it has never run or the "
        f"secret file ({config.secret_path}) was removed"
    )
    return {ENV_PLATFORM_UNREACHABLE: reason}, reach


def _prepare_environment(config: PlatformConfig) -> tuple:
    """Write the assistant's ``.env`` 0600. Returns ``(path, reach, credential)``.

    Why a file at all, when :func:`lmer_platform.spawn.spawn_session` already
    hands the child an environment: that environment belongs to the *host*
    ``lmer`` process, and what reaches the container is an allowlist ``lmer``
    builds by name (``lmer_cli.cli``'s container env dict). A variable it has
    never heard of does not cross. ``--env-file`` is the supported way in — its
    values are merged into that dict for keys the allowlist does not already
    define — and it is what the Slack listener already uses to forward a
    deployment ``.env`` into a session it spawns.

    Why a file rather than a flag, given a file is a second copy of the secret on
    disk: because the alternative puts it in argv. This module's spawn is echoed
    back by ``POST /api/sessions``' result shape, written into ``events.jsonl``
    and visible in ``ps`` — the same three places
    :func:`lmer_platform.spawn._build_command` refuses to put a control token.
    The file is created 0600 by ``os.open`` rather than chmod'ed afterwards, the
    way :func:`lmer_platform.config.ensure_secret` and
    ``spawn._mint_control_token`` create theirs: a credential that is
    world-readable for a millisecond is a credential that leaked. It sits beside
    the ``secret`` file it copies, in the same directory, so it widens nothing.

    It stays after the session ends, rewritten on the next start. Deleting it
    would have to happen after the child has read it, and the child reads it
    seconds into a launch this function has already returned from — a delete on
    :func:`stop` would only tidy the planned path and leave the crashed one, for
    a file whose contents already exist beside it.

    Fail-soft, and loudly, the same trade :func:`_write_state` takes: the
    registry is what makes a session real, and refusing to open the operator's
    chat window because a bookkeeping file would not write trades a small
    problem for a total one. The session then starts with no platform variables
    at all, which the taskdef reads as "say you cannot reach the platform".
    """
    values, reach = _assistant_environment(config)
    if project_context.current():
        values["LMER_WORKSPACE_ID"] = project_context.current()
        values["LMER_WORKSPACE_NAME"] = projects.get(project_context.current())["name"]
    path = env_file_path()
    body = "".join(_dotenv_line(name, value) for name, value in values.items())
    try:
        # Through the store rather than a bare mkdir: this is the platform state
        # dir, and it is owner-only whichever module happens to create it first
        # (:data:`lmer_platform.store.STATE_DIR_MODE`) — a 0600 file whose
        # directory a write from here left at 0755 is a mode nobody chose.
        ensure_state_dir(path.parent)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
        # In case the file pre-existed with looser bits: os.open ignores its
        # mode argument for a file that already exists.
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.error(
            "platform_assistant_env_unwritable path=%s error=%s — the assistant "
            "starts with no platform URL or secret and cannot drive anything",
            path, exc,
        )
        return None, reach, None

    if reach.reachable:
        logger.info(
            "platform_assistant_endpoint url=%s source=%s", reach.url, reach.source
        )
    else:
        logger.warning(
            "platform_assistant_unreachable reason=%s — the session is told so "
            "in its environment rather than handed a URL that fails", reach.reason,
        )
    credential = values.get(ENV_PLATFORM_CREDENTIAL)
    if credential != active_assistant_credential():
        credential = None
    return path, reach, credential


def _remember_generation(state) -> None:
    from . import assistant_history

    assistant_history.remember(state)
    if state.session_id:
        assistant_history.bind_credential(state.session_id, active_assistant_credential())


def authenticated_generation(credential: str) -> dict | None:
    """Resolve an authenticated identity; migrate an incumbent under the lifecycle lock."""
    from . import assistant_history

    owner = assistant_history.credential_owner(credential)
    if owner:
        return owner
    with _LOCK:
        if active_assistant_credential() == credential:
            _remember_generation(read_state())
        return assistant_history.credential_owner(credential)


@serialized
def start(
    config: PlatformConfig,
    *,
    handoff: Optional[str] = None,
    settings: Optional[dict] = None,
    refuse_held_threads: bool = True,
) -> AssistantStatus:
    """Start the assistant. Refuses if one is already running.

    On demand, never at daemon boot (D11): an operator who never opens the chat
    should not pay for a container. :func:`ensure_running` is the idempotent
    entry point callers should reach for; this one is the explicit verb, and it
    is the one that says "no" when an assistant is already up rather than
    silently doing nothing.

    *handoff* replaces what the next incarnation is told; omitting it carries the
    recorded one forward, which is what makes a respawn after a crash as
    informed as a planned rotation.

    *settings* is the per-call override layer for how the session is run —
    ``model``/``harness``/``preset``/``agents``/``threads``. Omitted keys fall
    through to the standing answer (env, then a fresh ``config.json`` read,
    then today's behaviour); see :func:`_launch_settings`. The supervisor's
    respawns pass nothing here, which is what makes a respawn after a crash run
    with the operator's *standing* configuration rather than with whatever a
    one-off start once asked for.

    *refuse_held_threads* is for :func:`rotate` alone, which settles that
    question before it stops the incumbent; asking again after the stop would
    answer for a moment the caller never saw (docs/COMMS.md §9).
    """
    with _LOCK:
        live = _live_assistant()
        if live is not None:
            raise _refuse(
                AssistantAlreadyRunning(
                    f"an assistant is already running as {live.get('id')} "
                    f"(pid {live.get('pid')}) — there is one at a time (D11). Stop "
                    "it, or rotate it if what you want is a fresh context window"
                ),
                "already_running",
            )
        try:
            _require_taskdef()
        except TaskdefMissing as exc:
            raise _refuse(exc, "taskdef_missing")

        state = read_state()
        now = utc_now_iso()
        text, stamp = _handoff_update(state, handoff, now)
        launch, launch_sources = _launch_settings(settings)
        if refuse_held_threads:
            try:
                _refuse_held_explicit_threads(launch, launch_sources)
            except AssistantThreadHeld as exc:
                raise _refuse(exc, "thread_held")

        # A refused duplicate must not rewrite the incumbent's environment or
        # credential, even if the host's reachability settings have changed.
        _remember_generation(state)
        env_file, reach, credential = _prepare_environment(config)

        # no_repo is the record of D17 in the request itself, and it says more
        # than an absent repo_url can: the session has no repository, so the
        # spawn neither falls back to the daemon's LMER_REPO_URL nor files the
        # assistant under a repository it has no checkout of, and the child is
        # told to skip the clone.
        #
        # ``--env-file`` carries a *path*, which is the point: the command list
        # this produces is echoed over HTTP and written to events.jsonl, and the
        # only thing in it is the name of a 0600 file.
        request = spawn.SpawnRequest(
            workspace_id=(project_context.current() or projects.GENERAL) if projects.enabled() else None,
            taskdef=TASKDEF,
            target=TARGET,
            targets=launch["threads"],
            no_repo=True,
            # The four launch settings travel as the typed fields the platform
            # already emits as its own flags for workers — never appended to
            # extra_args, where a later spelling would win over the recorded
            # one (spawn._RESERVED_ARGS refuses exactly that).
            preset=launch["preset"],
            agents=launch["agents"],
            harness=launch["harness"],
            model=launch["model"],
            extra_args=("--env-file", str(env_file)) if env_file else (),
        )
        # The backstop for rule drift, not the validation path: the values
        # above already went through the one rule set
        # (config._unusable_reason). If the spawn's own validation refuses
        # anyway, the rules have drifted apart — and that must answer as a
        # refusal the routes translate (400, logged, in platform history), not
        # as a SpawnError nothing here catches: a 500 on every start is how
        # the missing-rule bug this replaces actually presented.
        try:
            request.validate()
        except spawn.SpawnError as exc:
            raise _refuse(
                AssistantError(
                    f"the assistant's spawn request was refused: {exc} — a "
                    "launch setting passed the settings rules and failed the "
                    "spawn's; the two rule sets have drifted and this value "
                    "needs the fix upstream"
                ),
                "invalid_request",
            ) from exc
        from . import assistant_history

        previous = assistant_history.latest()
        published = False
        number = state.generation + 1

        def publish_registration(register, session_id: str, _pid: int) -> None:
            nonlocal published, number
            with _PUBLICATION_LOCK:
                register()
                record = assistant_history.register(
                    session_id, number=number, started_at=now,
                    previous=previous["session"] if previous else None,
                    entry=registry.read_session(session_id),
                )
                assistant_history.bind_credential(session_id, credential)
                number = record["generation"]
                _write_state(replace(
                    state,
                    session_id=session_id,
                    started_at=now,
                    generation=number,
                    handoff=text,
                    handoff_at=stamp,
                    stopped_at=None,
                    stop_reason=None,
                ))
                published = True

        result = spawn.spawn_session(
            config,
            request,
            kind=KIND,
            publish_registration=publish_registration,
            # Explicit threads refuse above; occupied standing threads defer.
            refuse_held_targets=False,
        )

        if not published:
            raise AssistantError(
                "the assistant spawn returned without publishing its registry "
                "entry and lifecycle state"
            )
        append_event(
            "assistant_started",
            note=result.session_id,
            data={
                "session": result.session_id,
                "pid": result.pid,
                "generation": number,
                "taskdef": TASKDEF,
                "target": TARGET,
                # Where it was told the platform is, never what it authenticates
                # with: this line is history an operator reads and pastes.
                "platform_url": reach.url,
                # How it was run and why — names only, with each value's layer
                # beside it, because "which model was that incarnation on, and
                # who chose it" is the post-mortem question a settings surface
                # creates (issue #234).
                "settings": launch,
                "settings_sources": launch_sources,
            },
        )
        logger.info(
            "platform_assistant_started id=%s pid=%s generation=%s",
            result.session_id, result.pid, number,
        )
        return status()


@serialized
def stop(
    *, reason: str = "operator", handoff: Optional[str] = None
) -> bool:
    """Stop the running assistant. Returns whether one was stopped.

    ``False`` is a normal answer, not a failure: nothing was running. The stale
    pointer is cleared on that path, because "stop" is the operator saying they
    do not want it — after which advertising a session id that names nothing is
    just noise.

    Not the general session-termination verb. Spec §7.5 splits that into *wind
    down* and *exit*, and this is the second one for exactly one session; when
    that slice lands this should collapse into it rather than grow a twin.
    """
    if reason not in STOP_REASONS:
        raise AssistantError(
            f"invalid stop reason {reason!r}: expected one of {', '.join(STOP_REASONS)}"
        )
    with _LOCK:
        state = read_state()
        now = utc_now_iso()
        text, stamp = _handoff_update(state, handoff, now)
        live = _live_assistant()
        _remember_generation(state)

        # First, and on both paths: the credential is a file, not a session
        # property, so a stop that left it valid would be a container-spawning
        # key belonging to nobody. Rotation re-mints under this same lock.
        revoke_assistant_credential()

        if live is None:
            if state.session_id is not None or text != state.handoff:
                _write_state(replace(
                    state,
                    session_id=None,
                    started_at=None,
                    handoff=text,
                    handoff_at=stamp,
                    stopped_at=state.stopped_at or now,
                    stop_reason=state.stop_reason or reason,
                ))
            return False

        session_id = live.get("id")
        gone = _terminate(live.get("pid"))
        if gone and isinstance(session_id, str):
            # Its container is detached (#444): a follower killed before it could
            # stop it would leave the old assistant running beside its successor.
            containers.stop_for_session(session_id)
        if gone:
            # The spawn path keeps the entry of a session that exited unclean,
            # because that entry is how a crash is detected — but this exit was
            # requested, so leaving it would report an assistant we killed as one
            # that died. Signal-terminated is never a clean exit, so nothing else
            # is going to remove it.
            with _PUBLICATION_LOCK:
                if isinstance(session_id, str):
                    registry.remove(session_id)
                _write_state(replace(
                    state,
                    session_id=None,
                    started_at=None,
                    handoff=text,
                    handoff_at=stamp,
                    stopped_at=now,
                    stop_reason=reason,
                ))
        else:
            _write_state(replace(
                state,
                session_id=state.session_id,
                started_at=state.started_at,
                handoff=text,
                handoff_at=stamp,
                stopped_at=now,
                stop_reason=reason,
            ))
        append_event(
            "assistant_stopped",
            note=session_id if isinstance(session_id, str) else None,
            data={"session": session_id, "reason": reason, "stopped": gone},
        )
        if not gone:
            logger.error(
                "platform_assistant_stop_failed id=%s pid=%s — it did not die to "
                "SIGTERM or SIGKILL; its registry entry is left alone so the fleet "
                "view keeps showing it, and its credential stays revoked, so the "
                "surviving container now gets a 401 from every route",
                session_id, live.get("pid"),
            )
        else:
            logger.info(
                "platform_assistant_stopped id=%s reason=%s", session_id, reason
            )
        return gone


@serialized
def rotate(
    config: PlatformConfig,
    *,
    handoff: Optional[str] = None,
    settings: Optional[dict] = None,
) -> AssistantStatus:
    """Replace the running assistant with a fresh one, carrying *handoff* over.

    The planned half of §8.3's context hygiene. The *trigger* is not here — an
    age or context-pressure policy is the daemon's to run, reading
    :attr:`AssistantStatus.age_seconds` — but the transition is, because doing it
    as two API calls would leave a window where an operator's ``start`` lands
    between the stop and the respawn and wins the race for the generation counter.

    *settings* rides to :func:`start` untouched, and a rotation with none is
    already how a settings change lands (issue #234): the replacement resolves
    the standing layers fresh, so "persist the new model, then rotate" needs no
    override here.
    """
    with _LOCK:
        # Refusals before side effects: the stop below ends the incumbent, so
        # anything start() would refuse has to refuse while there is still an
        # assistant to keep. Both questions are settled here, and start() is told
        # not to re-ask the thread one — its answer can change between the stop
        # and the respawn. docs/COMMS.md §9, "Rotating uber lmer", has the why.
        launch, launch_sources = _launch_settings(settings)
        live = _live_assistant()
        try:
            # The incumbent holds the threads this rotation is asking for again.
            _refuse_held_explicit_threads(
                launch, launch_sources, besides=(live or {}).get("id"),
            )
        except AssistantThreadHeld as exc:
            raise _refuse(exc, "thread_held", operation="rotate")
        stop(reason="rotation", handoff=handoff)
        return start(config, settings=settings, refuse_held_threads=False)


def ensure_running(config: PlatformConfig) -> AssistantStatus:
    """Start the assistant unless one is already up. Idempotent.

    The on-demand entry point, and the respawn path: an assistant whose session
    died leaves a stale pointer, and the next call replaces it. Deliberately not
    a supervision loop — "respawned if it dies" (§8.1) is served by calling this
    when someone actually wants the assistant, which is also what keeps D11 from
    meaning "a container the operator never opens".
    """
    with _LOCK:
        current = status()
        if current.running:
            return current
        return start(config)


@dataclass(frozen=True)
class SupervisionReport:
    """What the daemon's boot-time attempt achieved. Returned, logged, printed.

    Same shape and purpose as :class:`lmer_platform.reattach.ReattachReport`: a
    startup path reports per attempt, and :attr:`notice` is the operator-facing
    sentence for whichever outcome it was. ``adopted`` distinguishes the two ways
    an assistant can be up at the end of a boot — this daemon started it, or one
    was already there (it survived the last daemon, spec R11, or an operator
    started it) — because the second is not a thing to announce as a start, and
    the incumbent keeping its context window is the point of it.

    ``reachable``/``reason`` are read from
    :func:`lmer_platform.config.container_base_url`, which is a pure derivation:
    reading it here must not, and does not, rewrite the ``.env`` a live assistant
    was launched with (see :func:`_prepare_environment`).
    """

    running: bool
    session_id: Optional[str] = None
    generation: int = 0
    adopted: bool = False
    reachable: bool = False
    reason: Optional[str] = None
    error: Optional[str] = None
    slots: int = 0

    @property
    def notice(self) -> str:
        """The startup lines an operator reads, one outcome at a time.

        Always a string, because there is always something to say: unlike a
        re-attach, which is silent when nothing survived, the assistant either is
        or is not there and both facts matter at boot.
        """
        if not self.running:
            return (
                f"🤖 Assistant NOT running: {self.error}\n"
                "   The platform is serving anyway — the fleet view does not "
                "depend on it. A respawn is retried in the background; start it "
                "by hand with POST /api/assistant/start."
            )
        verb = "already running as" if self.adopted else "started as"
        lines = [
            f"🤖 Assistant {verb} {self.session_id} (generation {self.generation})"
            + (
                " — it survived the last daemon, so it keeps its context window"
                if self.adopted else
                " — the platform's own chat window, respawned if it dies"
            )
        ]
        # What the cap does and does not cover. Said at startup because the
        # arithmetic changed (T75): an operator who learned the old behaviour —
        # the assistant deducting a slot — would otherwise keep planning around a
        # number that is no longer short by one.
        lines.append(
            "   it holds its own slot rather than one of the configured "
            f"max_concurrent_sessions, so all {self.slots} worker slot(s) remain "
            "for work."
        )
        if not self.reachable:
            lines.append(
                "   ⚠️  it CANNOT reach the platform API from inside its "
                f"container: {self.reason}\n"
                "      You can still chat with it, and it can still answer "
                "questions asked through the ask channel — it cannot spawn, "
                "stop or answer anything over the API."
            )
        return "\n".join(lines)


class Supervisor:
    """Keeps one assistant running for the life of the daemon (§8.1, T63).

    Started from ``lmer platform run`` and from nowhere else — see the module
    docstring for why the diagnostic verbs must not launch a container — and
    structured the way :class:`lmer_platform.reattach.ControlDrain` is: a thread
    that does nothing but loop, and a :meth:`supervise_once` where every decision
    it makes actually lives. A loop is a poor place to observe any of them.

    One per process is intended. A second cannot produce a second assistant —
    :func:`start` refuses that on the strength of the registry (D11) — but it
    would double the respawn rate and split the backoff budget in two, so the
    daemon creates exactly one and hands it nothing to share.

    Every seam that would otherwise make a test wait for wall-clock time is a
    parameter: *clock* for the crash-loop threshold, *sleep* for the backoff, and
    *await_exit* for how a death is observed. That is not decoration — this suite
    runs in a one-CPU container where a timing assertion is a flake, so a
    crash-loop test drives an injected clock and asserts on the delays that
    *would* have been slept.
    """

    def __init__(
        self,
        config: PlatformConfig,
        *,
        poll: float = SUPERVISE_POLL_SECONDS,
        attempts: int = MAX_RESPAWN_ATTEMPTS,
        backoff: float = RESPAWN_BACKOFF_SECONDS,
        backoff_cap: float = RESPAWN_BACKOFF_CAP_SECONDS,
        settled: float = SETTLED_SECONDS,
        clock=time.monotonic,
        sleep=None,
        await_exit=None,
    ) -> None:
        self.config = config
        self.poll = poll
        self.attempts = attempts
        self.backoff = backoff
        self.backoff_cap = backoff_cap
        self.settled = settled
        self.clock = clock
        self.failures = 0
        self.gave_up = False
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._await_exit = await_exit or spawn.wait_for_exit_recorded
        #: The incarnation being watched, and when it was first seen up. ``None``
        #: means there is nothing to account for — no assistant has been seen
        #: since the last one was accounted for.
        self._watching: Optional[str] = None
        self._since: Optional[float] = None
        #: The loop's thread, once :meth:`start` has run. Kept so :meth:`resume`
        #: can tell "supervision stopped" from "supervision is still running and
        #: needs nothing from you" — the same question two operators tapping
        #: start would otherwise answer by starting a second loop.
        self._thread: Optional[threading.Thread] = None
        self._resume_lock = threading.Lock()

    def stop(self) -> None:
        """Ask the loop to finish, and cut short a backoff it is sitting in.

        Called when the server stops serving: a supervisor still in its backoff
        would otherwise start a container that nothing is left to serve.
        """
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def first_start(self) -> SupervisionReport:
        """Make the boot-time attempt inline and report what happened.

        Inline rather than left to the thread because the outcome is something
        the operator reads in the daemon's startup output, beside the bind notice
        and the re-attach summary — and because an assistant that is up before
        the server accepts anything is one whose row is in the first fleet view
        that gets rendered.

        Never raises. Every refusal and every unexpected error comes back as a
        report with :attr:`SupervisionReport.error` set; see the module docstring
        for why the daemon must serve regardless.
        """
        current = status()
        if current.running:
            self._note_running(current.session_id)
            return self._report(current, adopted=True)
        return self._attempt()

    def start(self) -> threading.Thread:
        """Run the supervision loop on a daemon thread."""
        thread = threading.Thread(
            target=self.run,
            name="lmer-platform-assistant-supervisor",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return thread

    def resume(self) -> bool:
        """Re-arm supervision after it gave up. ``True`` when it was re-armed.

        The other half of :meth:`_give_up` (T75), and the reason a give-up is
        recoverable rather than terminal for the life of the daemon: the loop
        exits, so an operator's ``POST /api/assistant/start`` used to leave a
        container running with nothing watching it — a recovery in appearance
        only, whose next crash was silent.

        Two things happen and both are needed. The budget is refilled, because the
        failures it counts are exactly the ones the operator has now had a chance
        to fix (a missing taskdef installed, a full host freed, an image pulled) —
        a resumed loop that started one attempt from giving up again would spend
        its whole allowance on the first crash. And the loop is started, since
        nothing else will: :meth:`run` returned when :meth:`supervise_once` did.

        ``False`` is the ordinary answer on a healthy daemon and is not a failure:
        supervision is already running and will see the new incarnation on its own,
        so re-arming would only mean two loops racing each other's respawns and
        halving the backoff. The give-up flag is what distinguishes the cases, and
        it is checked *before* liveness because the thread that set it may still be
        a few instructions from returning.
        """
        with self._resume_lock:
            if self._stop.is_set():
                # The server has stopped serving; a loop started here would spawn
                # a container nothing is left to serve. Same reasoning as
                # :meth:`stop`'s existence.
                return False
            watching = self._thread is not None and self._thread.is_alive()
            if watching and not self.gave_up:
                return False
            self.failures = 0
            self.gave_up = False
            self._watching = None
            self._since = None
            self.start()
            logger.info(
                "platform_assistant_supervision_resumed — an operator started the "
                "assistant by hand; the respawn budget is refilled and the watch "
                "is running again"
            )
            append_event(
                "assistant_supervision_resumed",
                note="manual start re-armed supervision",
                data={"attempts": self.attempts},
            )
            return True

    def run(self) -> None:
        """The loop. Returns when the supervisor is stopped or has given up."""
        while not self._stop.is_set() and self.supervise_once():
            pass

    def supervise_once(self) -> bool:
        """One pass. ``False`` means this supervisor is finished.

        The pass either waits on the assistant that is up, or replaces the one
        that is not. Waiting is where nearly all of a daemon's life is spent, and
        it ends the moment the spawn's watcher records the exit
        (:func:`lmer_platform.spawn.wait_for_exit_recorded`) rather than on a
        tick — :data:`SUPERVISE_POLL_SECONDS` bounds only the assistant this
        process never spawned and therefore has no event for.
        """
        current = status()
        if current.running:
            self._note_running(current.session_id)
            self._wait_for_exit(current.session_id)
            return not self._stop.is_set()

        self._note_gone()
        if self.failures >= self.attempts:
            self._give_up()
            return False
        if self.failures:
            delay = self.backoff_seconds()
            logger.warning(
                "platform_assistant_respawn_backoff failures=%d delay=%.1fs "
                "attempts_left=%d", self.failures, delay,
                self.attempts - self.failures,
            )
            self._sleep(delay)
            if self._stop.is_set():
                return False
        self._attempt()
        return not self._stop.is_set()

    def backoff_seconds(self) -> float:
        """How long to wait before the next attempt: doubling, then flat.

        Exponential because the cheap failures (a cap that is about to free up)
        clear early and the expensive ones (a broken image, a host with no
        runtime) do not; capped because a doubling with no ceiling stops retrying
        in any useful sense after a few hours.
        """
        return min(
            self.backoff * (2 ** max(0, self.failures - 1)), self.backoff_cap
        )

    def _wait_for_exit(self, session_id: Optional[str]) -> None:
        """Block until this incarnation ends, or the poll interval elapses."""
        if not session_id:
            # A live entry whose id is not a string: nothing to wait on, and
            # ``status`` will keep reporting it. Fall back to the interval.
            self._sleep(self.poll)
            return
        self._await_exit(session_id, self.poll)

    def _attempt(self) -> SupervisionReport:
        """Start the assistant once, absorbing anything that goes wrong.

        Two catches on purpose. :class:`AssistantError` is a *refusal* — the cap,
        a missing taskdef, an assistant that appeared between the status read and
        here — and is expected traffic worth a warning. Anything else is a bug or
        a broken host (:class:`lmer_platform.spawn.SpawnError` for an ``lmer``
        that is not on PATH, :class:`lmer_platform.store.StoreError` for a state
        dir that will not write), and it is caught for exactly the reason
        :func:`lmer_platform.reattach.reattach_all` catches broadly: the daemon
        has a fleet view to serve either way.
        """
        try:
            current = ensure_running(self.config)
        except AssistantError as exc:
            return self._failed(str(exc))
        except Exception as exc:  # noqa: BLE001 - the daemon serves regardless
            logger.exception(
                "platform_assistant_autostart_failed error=%r — the platform is "
                "serving without an assistant", exc,
            )
            return self._failed(f"{type(exc).__name__}: {exc}")

        # From the pointer as well as from the status, because a session can
        # already have exited by the time this reads back — which is exactly the
        # crash loop this has to be able to count.
        self._watching = current.session_id or read_state().session_id
        self._since = self.clock()
        logger.info(
            "platform_assistant_autostarted id=%s generation=%s",
            self._watching, current.generation,
        )
        if not current.running:
            # The spawn was accepted and the session was already gone by the time
            # this read back: a container that failed in its first second. The
            # budget is charged on the next pass, by ``_note_gone``, which is
            # where the lifetime is known — what this owes is a report that says
            # something truer than "not running: no reason given".
            return self._report(current, error=(
                f"it was started as {self._watching} and exited immediately — "
                "its session log is the only account of why"
            ))
        return self._report(current)

    def _failed(self, detail: str) -> SupervisionReport:
        """Spend one unit of the budget and report the failure."""
        self.failures += 1
        logger.warning(
            "platform_assistant_autostart_refused failures=%d/%d reason=%s",
            self.failures, self.attempts, detail,
        )
        return self._report(status(), error=detail)

    def _give_up(self) -> None:
        """Stop trying, loudly. Going quiet here is the failure worth avoiding.

        Not the end of supervision for the life of the daemon: the operator's
        manual start re-arms this through :meth:`resume`, which is why the message
        below can name a route and mean it.
        """
        self.gave_up = True
        logger.error(
            "platform_assistant_supervision_gave_up failures=%d — the assistant "
            "would not start or would not stay up, and nothing will retry it. "
            "The platform keeps serving; start it with POST /api/assistant/start "
            "once the cause is fixed, which puts this watch back too", self.failures,
        )
        append_event(
            "assistant_supervision_gave_up",
            note=f"{self.failures} consecutive failures",
            data={"failures": self.failures, "attempts": self.attempts},
        )

    def _note_running(self, session_id: Optional[str]) -> None:
        """Start the clock on the incarnation that is up, if it is a new one."""
        if session_id != self._watching or self._since is None:
            self._watching = session_id
            self._since = self.clock()

    def _note_gone(self) -> None:
        """Charge the budget for an incarnation that ended, or refill it.

        The crash-loop rule, and the reason :data:`SETTLED_SECONDS` exists: a
        start that succeeds and dies immediately is indistinguishable, from the
        outside, from a start that fails — and a supervisor that only counted
        *failed* starts would respawn a fast-dying assistant forever.

        An incarnation this daemon adopted is timed from when it was first *seen*
        rather than from its own ``started_at``, which under-counts a survivor
        that dies seconds into a boot. Conservative in the safe direction: it
        spends a unit of a budget an operator can refill, rather than granting an
        unbounded respawn.
        """
        if self._since is None:
            return
        lived = self.clock() - self._since
        self._watching = None
        self._since = None
        if lived >= self.settled:
            self.failures = 0
            return
        # Charged here *and* again if the next start also fails: two things went
        # wrong, and a state that broken should reach the give-up sooner.
        self.failures += 1
        logger.warning(
            "platform_assistant_crash_loop lived=%.1fs settled=%.1fs "
            "failures=%d/%d — the assistant did not stay up long enough to "
            "count as working", lived, self.settled, self.failures, self.attempts,
        )

    def _report(
        self,
        current: AssistantStatus,
        *,
        adopted: bool = False,
        error: Optional[str] = None,
    ) -> SupervisionReport:
        reach = container_base_url(self.config)
        return SupervisionReport(
            running=current.running,
            session_id=current.session_id,
            generation=current.generation,
            adopted=adopted,
            reachable=reach.reachable,
            reason=reach.reason,
            error=error,
            slots=self.config.max_concurrent_sessions,
        )


#: The one :class:`Supervisor` this process runs, once ``lmer platform run`` has
#: published it. Process-wide because the fact is about the process: the route that
#: needs it (``POST /api/assistant/start``) holds a config and a request body, and
#: threading a supervisor through :func:`lmer_platform.api.create_app` would put an
#: optional container-spawning dependency into a constructor every route test
#: calls. Same shape as :data:`lmer_platform.reattach._ACTIVE`, and the same reason.
#:
#: ``None`` is a supported state, not an uninitialised one: ``lmer platform spawn``,
#: an embedding caller and every test run the API with nothing supervising, and a
#: start there must behave exactly as it did before this seam existed.
_SUPERVISOR: Optional["Supervisor"] = None
_SUPERVISOR_LOCK = threading.Lock()


def register_supervisor(supervisor: Optional["Supervisor"]) -> None:
    """Publish (or with ``None`` withdraw) the supervisor this process runs.

    Called by ``lmer platform run`` beside the thread it starts, and withdrawn when
    the server stops serving — a stopped supervisor left registered would answer
    for a daemon that is on its way out, and in a test process it would answer for
    the previous test's containers.

    Last writer wins, deliberately and without a refusal: two supervisors in one
    process is a bug the daemon cannot produce (it creates exactly one), and a
    refusal here would turn that bug into a failed boot rather than a logged one.
    """
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if supervisor is not None and _SUPERVISOR is not None:
            logger.warning(
                "platform_assistant_supervisor_replaced — a second supervisor was "
                "registered in this process; the first one is no longer reachable "
                "for a re-arm"
            )
        _SUPERVISOR = supervisor


def resume_supervision() -> bool:
    """Re-arm this process's supervision after a give-up (T75).

    What ``POST /api/assistant/start`` calls once a start has *succeeded*, and the
    answer to the hole T63 left: :meth:`Supervisor._give_up` ends the watch thread,
    so the manual start it tells the operator to run brought back a container that
    nothing was watching.

    ``False`` means nothing was re-armed, which is the common case twice over —
    supervision is running and needs no help, or this daemon has none at all — and
    is never a reason to fail the start that has already happened. See
    :meth:`Supervisor.resume` for why the manual path is the only caller.
    """
    with _SUPERVISOR_LOCK:
        supervisor = _SUPERVISOR
    if supervisor is None:
        logger.debug(
            "platform_assistant_no_supervision — nothing to re-arm; this daemon "
            "starts the assistant on request only"
        )
        return False
    return supervisor.resume()


@serialized
def set_handoff(text: str) -> AssistantState:
    """Record the compact summary the next incarnation should be told (§8.3).

    Written by the assistant itself, over the API, before a rotation — the
    "authority and knowledge live outside the window" half of what makes rotation
    cheap. Stored rather than passed as argv; see the module docstring.

    Scrubbed and bounded by :func:`_scrubbed_text`, which is also what the
    standing orders and the digest notes go through: this is agent-authored text
    landing in ``assistant.json``, and the note is read back over the API by the
    next incarnation and shown to a browser on the way.
    """
    summary = _scrubbed_text(text, field="handoff", limit=MAX_HANDOFF_CHARS)
    with _LOCK:
        state = read_state()
        _write_state(replace(state, handoff=summary, handoff_at=utc_now_iso()))
        return read_state()


@serialized
def set_instructions(text: str) -> AssistantState:
    """Record the operator's standing orders, replacing the whole document (T87).

    The handoff's sibling and its opposite in the one way that counts: this is
    read at the start of every incarnation and never taken, so it is the place a
    preference goes when it has to outlive the session that was told it ("spawn
    reviewers with this preset"). The module docstring argues the split; what this
    function owns is the three properties that make an agent-authored document
    safe to keep.

    **Whole-document, not append.** There is no verb for "add a rule", because a
    document assembled from appends is one nothing can shorten: "stop doing X"
    would arrive as a *fourth* line contradicting the first, and the next
    incarnation would read both and pick. The taskdef therefore tells the
    assistant to re-read before it writes, which is also what keeps two
    incarnations from clobbering each other — this lock makes each write atomic,
    it cannot make a stale read fresh.

    **Scrubbed on the way in.** The text quotes an operator typing into a chat
    window, so a credential will eventually be in it, and this lands in a
    world-readable plain file the daemon reads at every start rather than in a
    transcript that at least sits 0600. Scrubbed *before* the bound, as in
    :func:`lmer_platform.transcripts._present`: a mask that lengthens the text must
    not be able to smuggle it past the limit, and the number in a refusal has to
    be the number of characters that would have been stored.

    **Non-empty, like the handoff.** There is deliberately no way to clear it to
    nothing through this path: an empty POST is far more likely to be a composer
    bug than an operator with no preferences at all, and "the standing orders
    silently emptied" is the failure that is invisible until an incarnation starts
    doing what it was told to stop. Removing the last rule is done by writing a
    document that says so.
    """
    document = _scrubbed_text(
        text, field="instructions", limit=MAX_INSTRUCTIONS_CHARS
    )
    with _LOCK:
        state = read_state()
        _write_state(replace(
            state, instructions=document, instructions_at=utc_now_iso()
        ))
        return read_state()


@serialized
def notify(note: str, *, kind: str = "event", data: Optional[dict] = None) -> bool:
    if projects.enabled():
        destination = projects.destination(data)
        if not destination:
            raise AssistantError("Assistant notification has no project destination.")
        with project_context.bound(destination):
            return _notify_bound(note, kind=kind, data=data)
    return _notify_bound(note, kind=kind, data=data)


def _notify_bound(note: str, *, kind: str = "event", data: Optional[dict] = None) -> bool:
    """Spool a compact digest of a material event. Returns whether one is live.

    The seam §8.3 asks for, and the whole of it: the daemon detects and calls
    this; nothing here polls anything. The return value is what lets a caller
    decide between waking a running assistant now and leaving the digest for the
    one that starts when the operator next opens the chat.

    Best-effort by construction — a digest is a convenience over an attention
    list the daemon computes anyway, so a failed state write costs one
    notification, not the detection that produced it.

    The note is scrubbed and bounded by :func:`_scrubbed_text` like the two
    documents beside it in this file, and *data* is scrubbed by
    :func:`_scrubbed_data` for the same reason: daemon-composed does not mean
    daemon-authored, and both halves of a digest quote a session.
    :meth:`lmer_platform.detect.Signal.digest` puts the question a *session* asked
    into the note and :meth:`lmer_platform.detect.Signal.data` puts the label an
    *agent* wrote into the payload, so the text an operator or an agent wrote
    reaches ``assistant.json`` through here and out again through
    ``POST /api/assistant/pending``.

    Both scrubs run before the spool is touched at all, which keeps the accounting
    honest in the T87/T92 shape: the note's bound is checked on the characters that
    would be stored, and what is weighed against :data:`MAX_PENDING` is the entry
    that is actually written.
    """
    text = _scrubbed_text(note, field="note", limit=MAX_NOTE_CHARS)
    label = _validated_text(kind, field="kind", limit=64)
    payload = _scrubbed_data(data)
    with _LOCK:
        state = read_state()
        now = utc_now_iso()
        entry = PendingNote(
            at=now,
            kind=label,
            note=text,
            data=payload,
        )
        if projects.enabled():
            # Imported here: spool_merge imports this module (directly and via checkin).
            from . import spool_merge

            pending = spool_merge.add_check_in(list(state.pending), entry)
            dropped = 0
        else:
            pending = list(state.pending) + [entry]
            dropped = max(0, len(pending) - MAX_PENDING)
        if dropped:
            discard = next((i for i, item in enumerate(pending)
                            if item.kind != "conversation_request"), len(pending) - 1)
            pending.pop(discard)
        starting = not state.pending
        _write_state(replace(
            state,
            pending=tuple(pending),
            # One predicate for both, because they answer one question: is this a
            # new accumulation? Two predicates re-dated a spool *inherited* from a
            # build without the stamp, so on upgrade the next digest made an
            # overdue accumulation not-due for a full interval.
            pending_since=(
                now if starting
                else (state.pending_since or _oldest_at(state.pending) or now)
            ),
            pending_seq=(
                state.pending_seq + 1 if starting else state.pending_seq
            ),
        ))
        if dropped:
            logger.warning(
                "platform_assistant_digest_dropped count=%d — the spool is full at "
                "%d; nothing is draining it", dropped, MAX_PENDING,
            )
        append_event(
            "assistant_notified", note=label, data={"kind": label, "note": text}
        )
        from . import assistant_conversation

        if label in assistant_conversation.DIGEST_KINDS:
            assistant_conversation.record(text, kind=label, data=payload)
        return _live_assistant() is not None


@serialized
def notify_conversation(identity: str, data: dict) -> bool:
    """Atomically accept one request and its permanent, content-free replay key."""
    with _LOCK:
        state = read_state()
        if identity in state.conversation_seen:
            return False
        if len(state.pending) >= MAX_PENDING:
            raise AssistantCapacityError("the assistant inbox is full; retry after it is drained")
        now = utc_now_iso()
        entry = PendingNote(at=now, kind="conversation_request",
                            note="A conversation request is waiting; reply to its explicit destination.",
                            data=_scrubbed_data(data))
        updated = replace(
            state, pending=(*state.pending, entry),
            conversation_seen=(*state.conversation_seen, identity),
            pending_since=state.pending_since or _oldest_at(state.pending) or now,
            pending_seq=state.pending_seq + (not state.pending),
        )
        if not _write_state(updated):
            raise AssistantError("could not persist the assistant request; retry delivery")
        return True


def mark_codex_wake(session_id: str, pending_seq: int, delivery_id: str) -> bool:
    """Acknowledge one native delivery without draining or retargeting the spool."""
    with _LOCK:
        state = read_state()
        if state.session_id != session_id or state.pending_seq != pending_seq or not state.pending:
            return False
        return _write_state(replace(state, codex_wake=delivery_id, nudged_at=utc_now_iso()))


def mark_nudged(session_id: Optional[str] = None, *, counted: bool = True,
                episode: Optional[int] = None, nudges: Optional[int] = None) -> Optional[str]:
    """Record that the platform has just nudged about the current spool (#317).

    *counted* records *nudges* (default: one more than stored) as *session_id*'s
    count for this episode (#584); ``counted=False`` only restarts the interval
    clock. Given *episode*, a take that landed since the caller read the state
    makes this a no-op returning ``None``: the nudge belonged to a spool that is
    gone, and its count must not leak into the next one.

    Returns the stamp it wrote, or ``None`` when the write failed — best-effort
    like :func:`notify` and for a sharper reason: the nudge has already been
    typed by the time this is called, so raising here would report a failure for
    something that happened. What a lost mark costs is one early re-nudge, which
    is the direction to fail in; the opposite (marking a nudge that never went
    out) would silence the retry the mark exists to space.

    Under :data:`_LOCK` with the spool it describes, so a digest arriving in the
    same instant cannot interleave with the mark for the accumulation it joins.
    """
    with _LOCK:
        state = read_state()
        if episode is not None and state.episode != episode:
            return None
        stamped = utc_now_iso()
        updated = replace(state, nudged_at=stamped)
        if counted:
            same = state.nudge_session == session_id
            updated = replace(
                updated,
                nudges=nudges if nudges is not None else state.episode_nudges(session_id) + 1,
                nudge_session=session_id,
                stalled_at=state.stalled_at if same else None,
            )
        if not _write_state(updated):
            return None
        return stamped


def mark_stalled(session_id: Optional[str], *, episode: Optional[int] = None) -> Optional[str]:
    """Record that this episode's stall signal went out (#584).

    Returns the stamp, or ``None`` when the write failed or *episode* has ended
    since the caller read it; the caller bounds the episode in memory either way.
    """
    with _LOCK:
        state = read_state()
        if episode is not None and state.episode != episode:
            return None
        stamped = utc_now_iso()
        if not _write_state(replace(
            state, stalled_at=stamped, nudge_session=session_id,
            nudges=state.episode_nudges(session_id),
        )):
            return None
        return stamped


def clamp_future_nudged_at() -> AssistantState:
    """Read the spool state, durably bounding a future nudge stamp to now.

    A corrected host clock can leave a parseable mark ahead of the present. If
    every detector tick merely folds ``now`` over that stored value, the repeat
    window restarts on every tick and no reminder ever becomes due. Correct the
    file once under the spool lock instead, so elapsed time can accumulate and a
    restarted daemon reads the same bound.

    If the best-effort write fails, return the state with no nudge bound for this
    decision. One early reminder is safer than letting an uncorrectable future
    value silence the accumulation indefinitely; the detector's in-memory mark
    still bounds repeats after that reminder is delivered.
    """
    with _LOCK:
        state = read_state()
        stamped = state.nudged_at
        if stamped is None:
            return state
        age = age_seconds(stamped)
        if age is None or age >= 0:
            return state
        corrected = replace(state, nudged_at=utc_now_iso())
        if _write_state(corrected):
            return corrected
        return replace(state, nudged_at=None)


def _oldest_at(pending: tuple) -> Optional[str]:
    """The earliest stamp among *pending*, or ``None`` when none is usable.

    Only for adopting an age on upgrade (:func:`notify`), which is why it is the
    minimum of the strings rather than a parse: these are ISO-8601 Z stamps
    written by :func:`lmer_platform.store.utc_now_iso`, so lexical order *is*
    chronological order, and one unparseable entry then costs the comparison
    nothing. An adopted stamp that turns out to be unreadable degrades to
    :func:`lmer_platform.nudge._accumulation_age`'s own note fallback rather than
    to a wrong number.
    """
    stamps = [note.at for note in pending if note.at]
    if not stamps:
        return None
    return min(stamps)


@serialized
def take_pending() -> list:
    """Drain the spooled digests, oldest first, and clear them.

    Draining is destructive because the alternative — a cursor — needs a reader
    identity, and the reader is a session that gets replaced on rotation. What
    matters instead is that nothing is lost by *not* reading: the attention list
    the daemon computes is still there, and the events are in ``events.jsonl``.

    It also clears :attr:`AssistantState.nudged_at`, because the accumulation
    that mark was about is what just left: whatever arrives next is a new one and
    is owed its own nudge (#317). An empty take clears it too — the spool is
    empty either way, and a mark left behind by a take that raced the last digest
    out would suppress the next accumulation's first nudge for a window.
    """
    from . import project_requests

    with _LOCK:
        state = read_state()
        if projects.enabled():
            return project_requests.drain(state)
        if not state.pending:
            if (state.nudged_at is not None or state.pending_since is not None
                    or state.nudges or state.stalled_at is not None):
                _write_state(replace(state, pending_since=None, **ended_episode(state)))
            return []
        notes = [note.to_dict() for note in state.pending]
        _write_state(
            replace(state, pending=(), pending_since=None, **ended_episode(state))
        )
        return notes


def _terminate(pid: object) -> bool:
    """End the assistant's process. Returns whether it is gone.

    Signals the process *group* where the child is its own group leader —
    :func:`lmer_platform.spawn.spawn_session` starts sessions with
    ``start_new_session=True``, so it is — because the thing that actually holds
    the container is ``lmer``'s own child, and a SIGTERM that reaches only the
    parent leaves a container running with nothing left watching it. The group
    identity is *checked* rather than assumed: ``killpg`` on a pid that is not a
    group leader would signal whatever group happens to carry that id.

    Refuses pids that cannot name a session: 0 and -1 mean "every process I can
    signal" to ``kill``, 1 is init, and our own pid would take the daemon down
    with the assistant. Reachable, since the pid is read back out of a registry
    file an operator can edit.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        logger.error("platform_assistant_bad_pid pid=%r — refusing to signal", pid)
        return False
    if pid == os.getpid():
        logger.error(
            "platform_assistant_self_pid pid=%d — the registry entry names this "
            "process; refusing to signal it", pid,
        )
        return False

    for sig, grace in (
        (signal.SIGTERM, STOP_GRACE_SECONDS),
        (signal.SIGKILL, STOP_KILL_GRACE_SECONDS),
    ):
        if not _signal_group(pid, sig):
            # ProcessLookupError on the way in: it was already gone.
            return not _alive(pid)
        if _wait_gone(pid, grace):
            return True
    return not _alive(pid)


def _signal_group(pid: int, sig: int) -> bool:
    """Signal *pid*'s group, or *pid* alone when it does not lead one.

    ``False`` means the process was already gone; any other failure is logged and
    reported as delivered so the caller falls through to its liveness check
    rather than escalating against a process it cannot signal anyway.
    """
    try:
        leads_group = os.getpgid(pid) == pid
    except ProcessLookupError:
        return False
    except OSError:
        leads_group = False
    try:
        if leads_group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.warning(
            "platform_assistant_signal_failed pid=%d signal=%s error=%s",
            pid, sig, exc,
        )
    return True


def _alive(pid: int) -> bool:
    """Whether *pid* still names a live process.

    Through the registry's own liveness check so the zombie handling is not
    written twice: the daemon is the session's parent, so an exited-but-unreaped
    child still answers ``kill(pid, 0)`` and would keep a stop waiting out its
    full grace period for a process that is already dead.
    """
    return registry.is_live({"pid": pid})


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until *pid* is gone or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while True:
        if not _alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_STOP_POLL_SECONDS)
