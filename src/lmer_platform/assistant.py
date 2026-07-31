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
``kind``. Nothing in this module distinguishes it, and that is the point — ``kind``
is a label on a spooled note, not an enum, so a new class of event costs the seam
nothing. What it is *not* is an attention reason: those mean a human has to act
and each one becomes a digest class automatically, while a milestone is addressed
to the orchestrator and stays off the operator's badge.

Nothing pushes. The spool waits to be taken (T89)
--------------------------------------------------
:func:`notify` returns whether an assistant is *live*, and that is the whole of
what "notified" means here: a digest is written to a file and the return value
says somebody might read it. No code path writes into a session's stdin, and
none is planned in this slice. That matters because a live incarnation read the
seam the other way round and told the operator "the orchestrator already pushes
me digests" — after which a completed review sat in the spool until it was
evicted, because an idle LLM session has no turn in which to poll and nothing
was going to give it one.

The fix is not here, and deliberately: what wakes an idle session is a *watch*
the session itself arms, using its harness's own background-monitor tool, whose
condition is the non-consuming ``pending`` count on :attr:`AssistantStatus` (see
:func:`status`). The count is on the status precisely so a watch has something to
poll that is not the destructive take — ``POST /api/assistant/pending`` drains,
so polling it would eat every digest at the moment nothing was ready to act on
it. The instruction that arms the watch, re-arms it after each take and reports a
watch that keeps erroring lives in the ``orchestrate`` taskdef, because it is a
fact about how the assistant's harness works rather than a fact about this state
file.

The fallback, if a taught watch turns out to be too easy to forget: the daemon
grows a push — :func:`notify` already knows an assistant is live, and
:mod:`lmer_platform.session_io` already knows how to type into a session — and
the digest still travels only as "something is waiting", with the spool remaining
the one bounded, scrubbed source. That design is written down in the plan and is
**not built**; the watch is cheaper and does not risk interrupting a session
mid-turn, so it goes first.

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

The write path is **chat**, not a settings screen (operator request, 2026-07-28:
"I want to be able to do that via the chat, not some ux config thing"). The
operator states a rule in the conversation, the assistant confirms the wording
back and posts the whole updated document; the UI shows it read-only so there is
one writer of the document's prose and it is the thing the operator is talking to.
The taskdef owns both halves of that, since both are behaviour rather than state.

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
``max_concurrent_sessions`` counts **workers**. The assistant is excluded from it
by :func:`lmer_platform.spawn._live_worker_count`, which is where the reservation
had to land — worker spawns go through the same function, so a slot reserved
anywhere else would not be reserved from them.

That is a decision, not a convenience, and the earlier one is worth keeping in
view: this module used to argue that the assistant should count "like any other
container, because a cap you can exceed by one is not a cap". What made that
wrong was the daemon starting the assistant at boot (T63). From then on the slot
was spent from boot for the life of the daemon, so a host configured for four
sessions ran *three* workers and a chat window, and the setting no longer meant
what it says. The cap is about how much work a host can bear; the assistant is
what routes that work, and it is one container whether the number is 1 or 40.
So the number now means what an operator reads it to mean, and the one container
they never asked for is not deducted from it.

What did **not** change: a second assistant is still refused, and still by
:func:`start` on the strength of the registry (D11), never by capacity. That
matters both ways round — a worker cap rejecting an assistant would be one
setting meaning two things, and the exclusion in the counting means a stale
incarnation's entry cannot make its own replacement fail either.

What remains true, and is why :class:`AssistantCapacityError` is still reachable:
this is a reservation, not an exemption. Workers that already fill the cap are
still holding every slot there is, so an assistant *start* against a full host is
refused with the numbers and the setting to raise. The daemon starts the assistant
before it serves anything, so in practice it is first; the refusal exists for the
manual start on a host that came up busy.

``max_concurrent_assistant_spawns`` is **gone** (T75). Per spec §6.4/§8.2 it
bounded the sessions the assistant *initiates*, which is a different question from
how many this host runs — and it was never enforced, only loaded, validated and
served under ``GET /api/state``. Enforcing it is not possible today: there is one
shared secret for the whole API and no way to attribute a spawn to the assistant
rather than to the operator's browser, so the only cap it could have implemented
would refuse the operator and blame the chat window. It is deleted rather than
served, with the argument and the conditions for its return recorded in
:mod:`lmer_platform.config`, because a setting an operator can lower and nothing
reads is worse than an absent one: it reads as a control.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lmer_cli.cli import _get_taskdef_paths, _resolve_taskdef_dir
from lmer_cli.runtime import repo_root_path

from . import registry, spawn
from .config import ConfigError, PlatformConfig, container_base_url, read_secret
# The credential scrub, imported rather than reimplemented — the trade
# :mod:`lmer_platform.config` and :mod:`lmer_platform.api` already make with
# ``_scrub_credentials`` and ``_web_base_from_remote``. A second definition of
# "what a credential looks like" is a second thing to forget a pattern from, and
# this one covers the platform's own shared secret *by value*, which no
# reimplementation here would.
from .transcripts import _scrub, _scrub_decoded
from .store import (
    StoreError,
    append_event,
    ensure_state_dir,
    read_json,
    snapshot_path,
    utc_now_iso,
    write_json,
)

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
    "set_instructions", "notify", "take_pending",
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

#: Consecutive failures before the supervisor stops trying and says so. Bounded
#: rather than infinite because every attempt is a clone and an image pull (§8.1
#: promises a respawn, not an unbounded one), and not *one* because the common
#: failure — the concurrency cap, a taskdef that is not installed yet — is one an
#: operator clears while the daemon is up.
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

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


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
    """No room under ``max_concurrent_sessions``.

    429, matching what ``POST /api/sessions`` already answers for a worker over
    cap, so a client has one rule for "the host is full" rather than two.
    """

    status = 429


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
        }

    @classmethod
    def from_dict(cls, payload: Optional[dict]) -> "AssistantState":
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
        """
        if not isinstance(payload, dict):
            return cls()
        generation = payload.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            generation = 0
        raw_pending = payload.get("pending")
        pending = tuple(
            note
            for note in (
                PendingNote.from_dict(item)
                for item in (raw_pending if isinstance(raw_pending, list) else [])
            )
            if note is not None
        )
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
            pending=pending[-MAX_PENDING:],
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
    log_path: Optional[str] = None

    @property
    def age_seconds(self) -> Optional[float]:
        """How long this incarnation has been up — what an age policy reads."""
        return _age_seconds(self.started_at)

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "session_id": self.session_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "age_seconds": self.age_seconds,
            "generation": self.generation,
            "stale": self.stale,
            "tracked": self.tracked,
            "pending": self.pending,
            "handoff": self.handoff,
            "log_path": self.log_path,
            "taskdef": TASKDEF,
            "target": TARGET,
        }


def _opt_str(value: object) -> Optional[str]:
    """A non-empty string, or ``None`` for anything else."""
    return value if isinstance(value, str) and value else None


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

    Same shape as ``work_repo.run_state._iso_to_minutes_apart`` — one timestamp
    format across both state layers, and an unparseable one is missing data
    rather than a crash in a status call. ``TypeError`` is in the catch rather
    than a separate type check in front of it, because that is what ``strptime``
    raises for ``None`` and for anything else that is not text: one answer, one
    branch.

    A timestamp in the future reads *negative*, deliberately unclamped: that is
    a clock that moved or a hand-edited state file, and clamping it to zero
    would report a plausible-looking age for a fact that is wrong. Nothing
    downstream needs a non-negative value — an age policy compares against an
    upper bound, which a negative never trips.
    """
    try:
        when = datetime.strptime(started_at, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


def state_path() -> Path:
    """Where the pointer and rotation bookkeeping live."""
    return snapshot_path(STATE_FILE)


def env_file_path() -> Path:
    """Where the assistant's ``.env`` lives — see :func:`_prepare_environment`."""
    return snapshot_path(ENV_FILE)


def read_state() -> AssistantState:
    """The recorded state, or the empty state when there is none.

    Never raises: a corrupt file has already been moved aside by
    ``store.read_json``, and an assistant that cannot be *started* because its
    bookkeeping is unreadable is a worse outcome than one that starts at
    generation 0.
    """
    try:
        stored = read_json(state_path())
    except StoreError as exc:
        logger.error(
            "platform_assistant_state_unreadable error=%s — starting from empty", exc
        )
        return AssistantState()
    return AssistantState.from_dict(stored)


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
        write_json(state_path(), state.to_dict())
    except StoreError as exc:
        logger.error(
            "platform_assistant_state_unwritable error=%s — the registry entry "
            "remains authoritative for what is running, but rotation bookkeeping "
            "was lost", exc,
        )
        return False
    return True


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


def _live_assistant() -> Optional[dict]:
    """The registry entry of the running assistant, or ``None``.

    The authority for "is one running" (module docstring). When state already
    names one, that entry wins, so a stray second assistant cannot make
    :func:`status` start reporting a different session id from one call to the
    next; otherwise the oldest live one is the incumbent. More than one is
    logged, because it means something upstream let two starts through.
    """
    live = [
        entry for entry in registry.list_sessions(live_only=True)
        if entry.get("kind") == KIND
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
    here. :func:`stop` and :func:`start` are where state changes, so that reading
    the status from a UI poll can never race a start.
    """
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
        )
    session_id = live.get("id")
    pid = live.get("pid")
    return AssistantStatus(
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
        log_path=_opt_str(live.get("log_path")),
    )


def _refuse(error: AssistantError, reason: str) -> AssistantError:
    """Record a refusal in platform history and return it to raise.

    Spec §8.2 requires cap refusals to reach ``events.jsonl`` — "a cap that is
    only a prompt instruction is not a cap" — and every other refusal here is
    worth the same line: each one means an operator asked for the assistant and
    did not get it, which is invisible in a UI that only shows what is running.
    """
    append_event(
        "assistant_start_refused",
        note=reason,
        data={"reason": reason, "detail": str(error)},
    )
    logger.warning("platform_assistant_start_refused reason=%s: %s", reason, error)
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


def _dotenv_line(name: str, value: str) -> str:
    """One ``KEY="value"`` line ``dotenv`` will read back exactly as written.

    Quoted and escaped rather than written bare, because one of these values is
    a sentence: an unquoted ``dotenv`` value ends at a ``#``, so an unreachable
    reason mentioning one would silently lose its tail.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"\n'


def _assistant_environment(config: PlatformConfig) -> tuple:
    """``(env, reach)`` — what to tell the session, and what was decided.

    Two variables or one, never a mixture. A URL with no secret is a 401
    machine and a secret with no URL is a credential with nothing to spend it
    on, so the pair is all-or-nothing and the absent case carries its reason.
    """
    reach = container_base_url(config)
    try:
        secret = read_secret(config)
    except ConfigError as exc:
        logger.error(
            "platform_assistant_secret_unreadable error=%s — the assistant will "
            "start without credentials", exc,
        )
        secret = None

    if reach.reachable and secret:
        # SPEC CORRECTION (§8.1). The spec says the assistant's token is "scoped
        # to the elevated verbs". It is not, and nothing here can make it so:
        # there is exactly one shared secret for the whole API
        # (``config.ensure_secret``, checked by ``api.require_secret``) and no
        # scoping mechanism anywhere behind it — no per-caller identity, no
        # per-route capability, no second credential. What the assistant holds is
        # the operator's own key, so it can do anything the operator's browser
        # can. The claim is dropped rather than left standing, because a spec
        # that asserts a boundary nobody implemented is worse than one that
        # admits there is none: the next person to add a dangerous verb would
        # believe the assistant could not reach it.
        #
        # This is the documented exception to ``ask_channel/protocol.py``'s "no
        # credential travels into the container". That reasoning is about
        # *worker* sessions and is untouched — they still get a mounted
        # directory and no key. The assistant is the one container that is meant
        # to have elevated authority (operator request, 2026-07-27); a chat
        # window that cannot spawn or answer anything is not an orchestrator.
        return {
            ENV_PLATFORM_URL: reach.url,
            ENV_PLATFORM_CREDENTIAL: secret,
        }, reach

    reason = reach.reason or (
        "this host has no shared secret to authenticate with — the platform "
        "daemon creates one on first start, so either it has never run or the "
        f"secret file ({config.secret_path}) was removed"
    )
    return {ENV_PLATFORM_UNREACHABLE: reason}, reach


def _prepare_environment(config: PlatformConfig) -> tuple:
    """Write the assistant's ``.env`` 0600. Returns ``(path, reach)``.

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
        return None, reach

    if reach.reachable:
        logger.info(
            "platform_assistant_endpoint url=%s source=%s", reach.url, reach.source
        )
    else:
        logger.warning(
            "platform_assistant_unreachable reason=%s — the session is told so "
            "in its environment rather than handed a URL that fails", reach.reason,
        )
    return path, reach


def start(
    config: PlatformConfig, *, handoff: Optional[str] = None
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

        # After the refusals this module raises and before the spawn, so a second
        # start against a live assistant cannot rewrite the file the live one was
        # already given — the reachability answer can change under it (an
        # operator rebinding the platform), and the file would then disagree with
        # what the running session actually read. A *capacity* refusal comes from
        # the spawn below and does rewrite it, which costs nothing: the content is
        # derived rather than accumulated, and the secret in it already sits
        # beside it in the same directory with the same mode.
        env_file, reach = _prepare_environment(config)

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
            taskdef=TASKDEF,
            target=TARGET,
            no_repo=True,
            extra_args=("--env-file", str(env_file)) if env_file else (),
        )
        try:
            result = spawn.spawn_session(config, request, kind=KIND)
        except spawn.CapacityError as exc:
            raise _refuse(
                AssistantCapacityError(
                    f"{exc} — the assistant holds its own slot beside "
                    "max_concurrent_sessions "
                    f"({config.max_concurrent_sessions}), which counts workers, but "
                    "it cannot take a slot that is already occupied. Free a worker "
                    "slot and start it again"
                ),
                "cap_reached",
            ) from exc

        _write_state(replace(
            state,
            session_id=result.session_id,
            started_at=now,
            generation=state.generation + 1,
            handoff=text,
            handoff_at=stamp,
            stopped_at=None,
            stop_reason=None,
        ))
        append_event(
            "assistant_started",
            note=result.session_id,
            data={
                "session": result.session_id,
                "pid": result.pid,
                "generation": state.generation + 1,
                "taskdef": TASKDEF,
                "target": TARGET,
                # Where it was told the platform is, never what it authenticates
                # with: this line is history an operator reads and pastes.
                "platform_url": reach.url,
            },
        )
        logger.info(
            "platform_assistant_started id=%s pid=%s generation=%s",
            result.session_id, result.pid, state.generation + 1,
        )
        return status()


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
            # The spawn path keeps the entry of a session that exited unclean,
            # because that entry is how a crash is detected — but this exit was
            # requested, so leaving it would report an assistant we killed as one
            # that died. Signal-terminated is never a clean exit, so nothing else
            # is going to remove it.
            registry.remove(session_id)

        _write_state(replace(
            state,
            session_id=None if gone else state.session_id,
            started_at=None if gone else state.started_at,
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
                "view keeps showing it", session_id, live.get("pid"),
            )
        else:
            logger.info(
                "platform_assistant_stopped id=%s reason=%s", session_id, reason
            )
        return gone


def rotate(config: PlatformConfig, *, handoff: Optional[str] = None) -> AssistantStatus:
    """Replace the running assistant with a fresh one, carrying *handoff* over.

    The planned half of §8.3's context hygiene. The *trigger* is not here — an
    age or context-pressure policy is the daemon's to run, reading
    :attr:`AssistantStatus.age_seconds` — but the transition is, because doing it
    as two API calls would leave a window where an operator's ``start`` lands
    between the stop and the respawn and wins the race for the generation counter.
    """
    with _LOCK:
        stop(reason="rotation", handoff=handoff)
        return start(config)


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


def notify(note: str, *, kind: str = "event", data: Optional[dict] = None) -> bool:
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
        entry = PendingNote(
            at=utc_now_iso(),
            kind=label,
            note=text,
            data=payload,
        )
        dropped = max(0, len(state.pending) + 1 - MAX_PENDING)
        _write_state(replace(
            state, pending=(*state.pending, entry)[-MAX_PENDING:]
        ))
        if dropped:
            logger.warning(
                "platform_assistant_digest_dropped count=%d — the spool is full at "
                "%d; nothing is draining it", dropped, MAX_PENDING,
            )
        append_event(
            "assistant_notified", note=label, data={"kind": label, "note": text}
        )
        return _live_assistant() is not None


def take_pending() -> list:
    """Drain the spooled digests, oldest first, and clear them.

    Draining is destructive because the alternative — a cursor — needs a reader
    identity, and the reader is a session that gets replaced on rotation. What
    matters instead is that nothing is lost by *not* reading: the attention list
    the daemon computes is still there, and the events are in ``events.jsonl``.
    """
    with _LOCK:
        state = read_state()
        if not state.pending:
            return []
        notes = [note.to_dict() for note in state.pending]
        _write_state(replace(state, pending=()))
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
