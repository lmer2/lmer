"""spawn-harness — run a launch-configured agent as a child harness process.

The in-container half of the ``--agents`` fan-out (issue #130): the host CLI
resolves ``--agents <name,...>`` against the operator's presets file and
forwards only the resolved per-agent config into the container as
``LMER_SPAWN_AGENTS_CONFIG`` (JSON ``{name: {"env": {...}, "prompt": "..."?}}``
— the env overlay plus an optional prompt preamble folded from the preset's
args; names also listed in ``LMER_SPAWN_AGENTS``). The container-side names are
scoped away from the host input ``LMER_AGENTS`` on purpose: they are ambient to
everything in the session, and under the input name a nested ``lmer`` inherited
the outer selection (issue #283). This tool lets the orchestrating agent run one
of those agents non-interactively::

    spawn-harness sol-review --prompt-file prompt.md \\
        --env LMER_REVIEW_ON_MR=0 --output agents/sol-review.md

One invocation runs one child and blocks until it exits; the orchestrating
agent parallelizes with its own background-shell tooling. Children are
stateless — no run dirs, no work-repo writes, no supervisor — and run with
their harness's permission checks bypassed (the lmer container is the
security boundary; see :class:`lmer_cli.harness.ExecProfile`).

Child environment: parent env, overlaid with the agent's preset ``env``,
overlaid with ``--env KEY=VAL`` pairs (last wins). Both fan-out pairs — the
scoped ``LMER_SPAWN_AGENTS`` / ``LMER_SPAWN_AGENTS_CONFIG`` and the host-input
``LMER_AGENTS`` / ``LMER_AGENTS_CONFIG`` — are stripped from the child so
children cannot fan out further (no grandchildren, structurally), and
``LMER_NONINTERACTIVE=1`` is set on every child. The rule the marker stands
for is delivered in-band, at the head of the child's prompt
(:data:`NONINTERACTIVE_NOTICE`), so a child reports a gate-worthy problem
instead of ending its turn on an unanswerable approval question whatever
harness it runs on (issue #137). The agent's own overlay
selects the harness (its ``LMER_HARNESS``, else the model hint from its
``LMER_LLM_NAME``, else the session's inherited harness — see
:func:`lmer_cli.harness.implied_harness_name`), and the effective child
env's ``LMER_LLM_NAME`` / ``LMER_REASONING_EFFORT`` become the harness's
model/effort flags (an inherited model foreign to a cross-harness child's
harness is dropped rather than passed through — see
:func:`select_harness`).

Unlike the fail-soft provisioning modules in this package, this is an
explicitly invoked tool: configuration problems fail loudly (exit 2), the
child's exit code is mirrored, and a ``--timeout`` expiry exits 124.

A child that succeeds while producing *nothing* is the one failure the exit
code cannot express: an empty, whitespace-only or stub-length ``--output``
warns on stderr and gets its own footer, while the real (zero) exit code is
still mirrored — see :func:`classify_degenerate_output` (issue #139). Whether
prose amounts to a *complete* answer is deliberately not guessed at here; that
function explains why.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Dict, Optional

from lmer_cli.harness import (
    HARNESS_ENV,
    LLM_NAME_ENV,
    NONINTERACTIVE_ENV,
    Harness,
    build_exec_argv,
    harness_for_model,
    implied_harness_name,
    known_harnesses,
    missing_credential_mounts,
)

# Both halves of the fan-out env contract live in lmer_cli.presets (the
# host-side writer) so the writer and this consumer can never drift apart.
# The host-input pair is imported for the child strip only — nothing here
# reads it, or a child could re-admit the ambient selection (issue #283).
from lmer_cli.presets import (
    AGENTS_CONFIG_ENV,
    AGENTS_ENV,
    SPAWN_AGENTS_CONFIG_ENV,
    SPAWN_AGENTS_ENV,
)
from lmer_cli.user_harnesses import CONTAINER_HARNESS_CACHE_DIR

#: Exit code for a child killed by ``--timeout`` (the coreutils convention).
TIMEOUT_EXIT_CODE = 124

#: Default seconds between "still running" heartbeat lines on stderr.
DEFAULT_HEARTBEAT_SECONDS = 60.0

#: How many trailing stderr lines the failure footer preserves.
STDERR_TAIL_LINES = 40

#: Below this many non-whitespace-padded characters, a successful child's
#: output counts as degenerate. Deliberately low: a legitimately terse answer
#: ("no findings", 11 characters) must survive, so the floor only catches
#: stubs like "ok" or "n/a" (issue #139).
DEGENERATE_MIN_CHARS = 10

#: In-band non-interactive notice, prepended to every child prompt.
#:
#: :data:`~lmer_cli.harness.NONINTERACTIVE_ENV` marks the child *process*, but
#: an environment variable reaches no model on its own — and the ``AGENTS.md``
#: rule it keys on does not reach every child either: a claude child execs the
#: binary directly with no runner script, and Claude Code discovers only
#: ``CLAUDE.md`` natively, so nothing appends ``AGENTS.md`` to its system
#: prompt (``libexec/claude-runner.sh`` does that for full sessions). Session
#: launches get the same text from the ``prompts/non-interactive.md`` fragment;
#: children get it here, in the one channel every harness reads the same way
#: (issue #137).
NONINTERACTIVE_NOTICE = (
    "[non-interactive session] No human is attached to this run and nobody "
    "can answer a question. Do not end your turn asking for approval or "
    "confirmation: if a gate would stop you for approval you were not already "
    "given, state in your final output what you would have asked, why you "
    "stopped, and what you completed — and do not perform the gated action "
    "either. Approval the prompt below already carries is still approval."
)


def _fail(message: str) -> "SystemExit":
    print(f"❌ {message}", file=sys.stderr)
    return SystemExit(2)


def load_agents_config(environ: Dict[str, str]) -> Dict[str, dict]:
    """Parse ``LMER_SPAWN_AGENTS_CONFIG`` from ``environ``.

    Returns the ``{name: {"env": {...}}}`` mapping. Absent/empty means no
    agents were configured at launch. A malformed value is a hard error —
    the variable is machine-written by the host CLI, so damage means the
    session is misconfigured, not that the user typo'd.

    Only the scoped name is read: falling back to ``LMER_AGENTS_CONFIG``
    would re-admit exactly the ambient value the scoping removed (#283).
    """
    raw = environ.get(SPAWN_AGENTS_CONFIG_ENV, "").strip()
    if not raw:
        return {}
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _fail(f"{SPAWN_AGENTS_CONFIG_ENV} is not valid JSON: {exc}") from None
    if not isinstance(config, dict):
        raise _fail(
            f"{SPAWN_AGENTS_CONFIG_ENV} must be a JSON object, "
            f"got {type(config).__name__}"
        )
    for name, entry in config.items():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("env", {}), dict)
            or not isinstance(entry.get("prompt", ""), str)
        ):
            raise _fail(f"{SPAWN_AGENTS_CONFIG_ENV} entry {name!r} is malformed")
    return config


def resolve_agent(name: str, config: Dict[str, dict]) -> dict:
    """Return the config entry for ``name``, failing loudly when unknown."""
    if not config:
        raise _fail(
            "No agents configured — launch the session with "
            f"--agents <name,...> (or {AGENTS_ENV}) to enable spawn-harness; "
            f"the launch forwards the resolved selection as "
            f"{SPAWN_AGENTS_CONFIG_ENV}"
        )
    if name not in config:
        available = ", ".join(sorted(config))
        raise _fail(f"Unknown agent {name!r} (available: {available})")
    return config[name]


def parse_env_pairs(pairs: list) -> Dict[str, str]:
    """Parse repeatable ``--env KEY=VAL`` arguments."""
    parsed: Dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise _fail(f"--env expects KEY=VAL, got {pair!r}")
        parsed[key] = value
    return parsed


def build_child_env(
    parent_env: Dict[str, str],
    agent_env: Dict[str, str],
    extra_env: Dict[str, str],
) -> Dict[str, str]:
    """Compose the child environment (parent < agent preset < ``--env``).

    The fan-out variables are stripped so a child cannot spawn further
    children — the no-grandchildren rule is structural, not advisory. Both
    spellings go: the scoped pair this tool reads, and the host-input pair,
    which a child would otherwise carry into any ``lmer`` it runs (#283).

    ``LMER_NONINTERACTIVE`` is set last, so neither the preset overlay nor an
    ``--env`` pair can unset it: a child harness process has no human attached
    by construction (issue #137). The variable declares the fact to anything
    the child itself spawns or shells out to; what steers the child's *own*
    agent is :data:`NONINTERACTIVE_NOTICE`, carried in the prompt, because no
    environment value reaches a model's context on its own. A child that ends
    its turn on ``Shall I proceed? (yes/no)`` exits 0 with a near-empty output
    file, so the orchestrator consolidates from N-1 agents with nothing to
    show it lost one.
    """
    child = dict(parent_env)
    child.update(agent_env)
    child.update(extra_env)
    for key in (
        AGENTS_ENV,
        AGENTS_CONFIG_ENV,
        SPAWN_AGENTS_ENV,
        SPAWN_AGENTS_CONFIG_ENV,
    ):
        child.pop(key, None)
    child[NONINTERACTIVE_ENV] = "1"
    return child


def select_harness(
    child_env: Dict[str, str],
    overlay_env: Dict[str, str],
    parent_env: Optional[Dict[str, str]] = None,
) -> Harness:
    """Select the child's harness and write it back into ``child_env``.

    The orchestrating session's container always carries the *parent's*
    resolved ``LMER_HARNESS``, so an inherited value must not shadow the
    agent's own configuration. The precedence (overlay ``LMER_HARNESS`` >
    model hint from the overlay's own ``LMER_LLM_NAME`` > the inherited
    parent harness > ``claude``) lives in
    :func:`lmer_cli.harness.implied_harness_name`, shared with the host
    CLI's launch-time credential-mount computation (issue #131). An unknown
    explicit name fails loudly.

    The selected name is written back to ``child_env[LMER_HARNESS]`` so the
    child process sees a value consistent with what actually runs. And when
    the agent's overlay supplies no model of its own, an *inherited*
    ``LMER_LLM_NAME`` whose family implies a harness other than a
    cross-harness child's is dropped from ``child_env``: a harness-only
    agent (``{"env": {"LMER_HARNESS": "codex"}}``) spawned from a session
    running e.g. ``LMER_LLM_NAME=fable`` must not hand the claude-family
    model to a codex child as ``--model`` (the child runs its harness's
    default model instead). A child on the session's own harness always
    keeps the model (it runs there by construction — e.g. a ``--harness
    pi`` session driving an Anthropic model via API keys), as does a model
    that implies the selected harness or implies nothing (custom registry
    names are ambiguous, never dropped). ``parent_env`` supplies the
    session's own harness for that exemption; it defaults to ``child_env``,
    which is only equivalent while the overlay carries no ``LMER_HARNESS``
    of its own — real callers pass the parent environment.
    """
    name = implied_harness_name(overlay_env, child_env)
    registry = known_harnesses()
    if name not in registry:
        known = ", ".join(sorted(registry))
        raise _fail(f"Unknown harness {name!r} for agent (known harnesses: {known})")
    if not (overlay_env.get(LLM_NAME_ENV) or "").strip():
        inherited_model = child_env.get(LLM_NAME_ENV)
        session_name = (
            ((parent_env if parent_env is not None else child_env).get(HARNESS_ENV) or "")
            .strip()
            .lower()
        )
        implied = harness_for_model(inherited_model)
        if (
            inherited_model
            and name != session_name
            and implied is not None
            and implied != name
        ):
            child_env.pop(LLM_NAME_ENV, None)
    child_env[HARNESS_ENV] = name
    return registry[name]


def apply_harness_extra_env(child_env: Dict[str, str], harness: Harness) -> None:
    """Merge the selected harness's fixed environment into the child env.

    ``Harness.extra_env`` is the harness's *fixed environment* (e.g. pi's
    ``PI_SKIP_VERSION_CHECK``, a user manifest's ``extra_env``). At session
    launch ``cli.py`` applies it for the session harness only — a fan-out
    child routed to a *different* harness execs the binary directly (no
    runner script to export it), so it must be merged here. ``setdefault``
    keeps the same lose-to-existing precedence as the launch-time env dict:
    an inherited/overlay/``--env`` value wins over the registry default.
    """
    for key, value in harness.extra_env:
        child_env.setdefault(key, value)


def prepend_user_harness_path(child_env: Dict[str, str], harness: Harness) -> None:
    """Put a user-installed child harness's install cache on the child PATH.

    Fan-out children run the harness binary directly — ``runner.sh`` (which
    owns install-if-missing for user harnesses) never runs for them. The
    documented cache convention is binaries under
    ``/lmer-harness-cache/<name>/bin`` (docs/HARNESSES.md), so prepending
    that directory makes a previously-installed user harness reachable as a
    child. If nothing is installed there the child still fails with a clear
    'command not found'. No-op for built-ins (baked into the image).
    """
    if harness.source_dir is None:
        return
    cache_bin = f"{CONTAINER_HARNESS_CACHE_DIR}/{harness.name}/bin"
    path = child_env.get("PATH", "")
    if cache_bin not in path.split(":"):
        child_env["PATH"] = f"{cache_bin}:{path}" if path else cache_bin


def warn_missing_credentials(
    agent_name: str, harness: Harness, parent_env: Dict[str, str]
) -> None:
    """Warn when a non-session child harness has no credential file mounted.

    The host mounts credential files for every harness the ``--agents``
    selection implies at launch (issue #131) — but a ``--env LMER_HARNESS=…``
    / ``--env LMER_LLM_NAME=…`` pair can reroute a child at spawn time to a
    harness the launch computation never saw, whose credentials were never
    mounted. Same contract as the launch-time check: a warning on stderr,
    never an error (keys-via-env harnesses can authenticate without the
    file, and a mounted file is no promise of working auth).

    The session's own harness is exempt — its credential state is the
    session's, already visible at launch.
    """
    session_name = (parent_env.get(HARNESS_ENV) or "").strip().lower()
    if harness.name == session_name:
        return
    missing = missing_credential_mounts(
        harness, lambda cred: os.path.exists(cred.container_path)
    )
    if missing:
        paths = " / ".join(cred.container_path for cred in missing)
        print(
            f"⚠️  agent {agent_name!r} runs on {harness.name} but "
            f"{paths} is not mounted — the child may fail to authenticate "
            f"(log in to {harness.name} on the host, and include it in "
            "--agents so its credentials mount)",
            file=sys.stderr,
        )


def resolve_prompt(ns: argparse.Namespace, agent: dict) -> str:
    """Resolve the child's effective prompt from the caller and the preset.

    A preset-supplied prompt (its args' ``--prompt``, folded at launch) is a
    preamble: prepended to the caller's ``--prompt``/``--prompt-file`` text,
    or used alone when the caller supplies none — a canned persona can be a
    complete task. An empty caller prompt (or none at all with no preamble)
    fails loudly.

    :data:`NONINTERACTIVE_NOTICE` leads the result, ahead of the preset's own
    preamble: the prompt is the only channel every harness reads identically,
    so it is where the "nobody can answer you" rule is guaranteed to land
    (issue #137). Emptiness is judged on the caller's and preset's text alone —
    the notice never turns an empty prompt into a valid one.
    """
    preamble = agent.get("prompt", "")
    if ns.prompt is None and ns.prompt_file is None:
        if not preamble.strip():
            raise _fail("A prompt is required: --prompt or --prompt-file")
        return f"{NONINTERACTIVE_NOTICE}\n\n{preamble}"
    if ns.prompt_file is not None:
        try:
            with open(ns.prompt_file) as handle:
                prompt = handle.read()
        except OSError as exc:
            raise _fail(f"Cannot read --prompt-file: {exc}") from None
    else:
        prompt = ns.prompt
    if not prompt.strip():
        raise _fail("The prompt is empty")
    if preamble.strip():
        prompt = f"{preamble}\n\n{prompt}"
    return f"{NONINTERACTIVE_NOTICE}\n\n{prompt}"


def _pump_stderr(stream, tail: "deque") -> None:
    """Pass the child's stderr through to ours, keeping a tail for the
    failure footer."""
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace")
        sys.stderr.write(line)
        tail.append(line)
    stream.close()


def _failure_footer(handle, reason: str, tail: "deque") -> None:
    """Append a machine-spottable failure record to the output file.

    A dead child would otherwise leave an empty/partial output file whose
    only failure signal is our exit code — the actual error lives in the
    child's stderr, which the orchestrator's background shell may never
    surface. (Field finding from the first real multi-agent review run.)
    """
    handle.write(f"\n---\n[spawn-harness] child FAILED: {reason} — ")
    handle.write("output above may be empty or partial\n")
    # Snapshot before iterating: the pump thread may still be appending (a
    # grandchild holding the stderr pipe open past the kill outlives the
    # 5s pump join), and iterating a mutating deque raises RuntimeError.
    # list(deque) is atomic under the GIL.
    tail = list(tail)
    if tail:
        handle.write("[spawn-harness] stderr tail:\n")
        handle.writelines(tail)


def _write_failure_footer(
    handle, reason: str, tail: "deque", output: Optional[str] = None
) -> None:
    """Write the failure footer, warning instead of raising when it cannot.

    Every caller is past the child's exit, and :func:`run_child` mirrors that
    exit code — the one contract ``spawn-harness`` promises its callers. On a
    full or read-only filesystem, letting our own inability to write a note
    escape would replace the code the caller needs (a timeout's
    :data:`TIMEOUT_EXIT_CODE`, a failed child's own code) with a traceback from
    the harness, on exactly the paths where an accurate code matters most
    (issue #151). So the failure is reported the same way
    :func:`warn_degenerate_output` reports its own: a stderr warning, which the
    orchestrating agent's background shell surfaces, and an unchanged exit code.

    The ``flush()`` is part of the guard, not an afterthought: writes into a
    buffered handle frequently succeed while the filesystem is full, and the
    ``ENOSPC`` surfaces only when the buffer drains. Flushing here catches it
    where it can still be absorbed.
    """
    try:
        _failure_footer(handle, reason, tail)
        handle.flush()
    except OSError as exc:
        where = f" to {output}" if output else ""
        print(
            f"⚠️  spawn-harness: cannot append the failure footer{where}: {exc}",
            file=sys.stderr,
        )


def _close_output(handle, output: Optional[str] = None) -> None:
    """Close the captured-output handle, warning instead of raising.

    The tail end of :func:`_write_failure_footer`'s reasoning: a buffered
    handle can hold a doomed write until ``close()`` flushes it, so an
    unguarded close would clobber the mirrored exit code one line after the
    footer guard saved it.
    """
    try:
        handle.close()
    except OSError as exc:
        where = f" {output}" if output else ""
        print(
            f"⚠️  spawn-harness: cannot close --output{where}: {exc}",
            file=sys.stderr,
        )


def classify_degenerate_output(content: str) -> Optional[str]:
    """Return why ``content`` is unusable as a child's answer, else ``None``.

    A child that *dies* announces itself through its exit code and the failure
    footer. A child that exits 0 having produced nothing useful announces
    nothing at all: the orchestrator consolidates from N-1 agents and only the
    output file's size hints that a result went missing (issue #139, the shape
    observed in #137). This classifier is the detection half of the warning —
    pure, so the same signals can be asserted directly.

    Three signals, cheapest first: an empty file, whitespace-only content, and
    content below :data:`DEGENERATE_MIN_CHARS`. All three are properties of the
    bytes, not of what the child meant — a file with nothing in it is missing an
    answer whatever the child was thinking.

    **Deliberately absent: any judgment of whether prose is a complete answer.**
    #137's child halted by asking permission, and earlier versions of this
    function tried to catch that shape by matching the last line (approval
    phrases, ``(y/n)`` spellings, a terminal ``?``, graded by output length).
    Every variant reduced to guessing intent from phrasing, and a child can halt
    for any number of reasons, phrased any number of ways — an open question with
    no yes/no framing, or no question at all. Nothing separates a halt from a
    legitimately terse answer structurally: ``Do you want me to apply the
    patch?`` (34 characters, a halt) and ``Looks fine. Should we also check the
    retry path?`` (47, an answer) are the same shape at the same size. The
    harness's own result envelope does not settle it either — a model that stops
    to ask still ends its turn normally and reports success — so the question is
    a judgment about content, which needs a model rather than a heuristic. See
    issue #153; #138 covers the hook-side session signal.
    """
    if not content:
        return "output file is empty"
    stripped = content.strip()
    if not stripped:
        return "output file is whitespace only"
    if len(stripped) < DEGENERATE_MIN_CHARS:
        return (
            f"output is {len(stripped)} characters, below the "
            f"{DEGENERATE_MIN_CHARS}-character floor"
        )
    return None


def _degenerate_footer(handle, reason: str) -> None:
    """Append the exited-0-with-nothing-usable record to the output file.

    A marker of its own: the child's exit code genuinely was 0, and an
    orchestrator scanning for ``child FAILED`` must be able to tell "the agent
    died" from "the agent returned nothing". No stderr tail either — a healthy
    child's diagnostics say nothing about why its answer is missing, and the
    footer's job is to keep the file readable, not to reprint noise.
    """
    handle.write(f"\n---\n[spawn-harness] child produced NO USABLE OUTPUT: {reason} — ")
    handle.write("the child exited 0, so this is a dropped result, not a failure\n")


def warn_degenerate_output(agent_name: str, output: str) -> Optional[str]:
    """Warn when a successfully exited child's ``output`` file is degenerate.

    Reads the captured file back, classifies it, and — on a hit — prints a
    stderr warning in the same family as the heartbeat and credential lines
    (the orchestrating agent's background shell surfaces stderr; a footer
    alone would only be found by opening the file) and appends the footer.
    Returns the reason, or ``None`` when the output looks usable. The exit code
    is the caller's business and stays the child's own: a fan-out that treated
    a terse answer as a hard failure would be worse than the bug this catches.

    Only the byte-level signals in :func:`classify_degenerate_output` reach
    here; a file with real content in it is left alone, however that content
    reads. An unreadable output file is not a degenerate child either — that is
    our own I/O problem, so it warns and returns ``None`` rather than accusing
    the agent.
    """
    try:
        with open(output, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError as exc:
        print(
            f"⚠️  spawn-harness: cannot re-read --output {output} to check "
            f"agent {agent_name!r}'s result: {exc}",
            file=sys.stderr,
        )
        return None
    reason = classify_degenerate_output(content)
    if reason is None:
        return None
    print(
        f"⚠️  spawn-harness: agent {agent_name!r} exited 0 but produced no "
        f"usable output — {reason} ({output}); the result is missing, not empty "
        "by choice — re-run the agent or drop it from the consolidation "
        "explicitly",
        file=sys.stderr,
    )
    # Guarded rather than left to propagate: the child has already exited 0 and
    # run_child mirrors that code, so failing to write a note must not become a
    # changed exit status. Same contract as _write_failure_footer, reached
    # through its own `with open(...)` because this footer appends by path
    # rather than through run_child's still-open handle.
    try:
        with open(output, "a", encoding="utf-8") as handle:
            _degenerate_footer(handle, reason)
    except OSError as exc:
        print(
            f"⚠️  spawn-harness: cannot append the no-usable-output footer to "
            f"{output}: {exc}",
            file=sys.stderr,
        )
    return reason


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the child's whole process group and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


def _raise_sigterm(signum, frame) -> None:
    """SIGTERM → SystemExit(143) so the wait loop's cleanup path runs."""
    raise SystemExit(128 + signum)


def run_child(
    argv: list,
    child_env: Dict[str, str],
    output: Optional[str],
    timeout: Optional[float],
    heartbeat: float = DEFAULT_HEARTBEAT_SECONDS,
    agent_name: str = "child",
) -> int:
    """Run the child process, capturing stdout to ``output`` when given.

    Without ``--output`` the child's stdout flows to ours; stderr always
    passes through for diagnosis (and its tail is appended to the output
    file as a failure footer when the child dies, so a failed agent's
    output file explains itself). While the child runs, a heartbeat line is
    printed to stderr every ``heartbeat`` seconds (0 disables) — harnesses
    buffer their final answer, so the output file staying empty says
    nothing about liveness. A child that exits 0 having written nothing
    usable is warned about instead (see :func:`warn_degenerate_output`);
    ``agent_name`` only names the child in that warning. Returns the
    child's exit code — a signal-killed
    child maps to the shell convention ``128 + N``, a ``--timeout`` expiry
    to :data:`TIMEOUT_EXIT_CODE`. The child gets its own session so a
    timeout kill takes its whole process group down with it (a surviving
    grandchild could keep writing the output file after we report it
    final) — which also detaches it from terminal signal delivery, so an
    interrupted wrapper (operator Ctrl-C, orchestrator cancelling the
    fan-out with SIGTERM) performs the same group kill before exiting
    ``128 + N``: a cancelled fan-out must not leave the child running to
    natural completion, still writing an output file the orchestrator
    believes is final. An unwritable output path, a missing harness binary,
    or an oversized argv fail loudly (exit 2) like every other
    configuration problem — but once the child has run, the mirrored code
    wins over our own I/O trouble: a footer or close we cannot complete
    warns and leaves the code alone (:func:`_write_failure_footer`).
    """
    try:
        stdout = open(output, "w") if output else None
    except OSError as exc:
        raise _fail(f"Cannot write --output: {exc}") from None
    stderr_tail: deque = deque(maxlen=STDERR_TAIL_LINES)
    try:
        try:
            proc = subprocess.Popen(
                argv,
                env=child_env,
                stdout=stdout,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise _fail(f"Cannot run {argv[0]!r}: {exc}") from None
        pump = threading.Thread(
            target=_pump_stderr, args=(proc.stderr, stderr_tail), daemon=True
        )
        pump.start()
        try:
            # SIGTERM's default handler would exit without unwinding — map it
            # to SystemExit so the interrupt cleanup below runs. Restored on
            # exit; unavailable outside the main thread (embedded callers).
            previous_sigterm = signal.signal(signal.SIGTERM, _raise_sigterm)
        except ValueError:
            previous_sigterm = None
        started = time.monotonic()
        timed_out = False
        try:
            while True:
                elapsed = time.monotonic() - started
                remaining = None if timeout is None else timeout - elapsed
                if remaining is not None and remaining <= 0:
                    timed_out = True
                    break
                tick = heartbeat if heartbeat > 0 else remaining
                if tick is not None and remaining is not None:
                    tick = min(tick, remaining)
                try:
                    code = proc.wait(timeout=tick)
                    break
                except subprocess.TimeoutExpired:
                    if heartbeat > 0:
                        print(
                            f"⏳ spawn-harness: child still running "
                            f"({time.monotonic() - started:.0f}s elapsed)",
                            file=sys.stderr,
                        )
        except BaseException as exc:
            _kill_process_group(proc)
            pump.join(timeout=5)
            print("❌ spawn-harness interrupted — child killed", file=sys.stderr)
            if stdout:
                # Narrow on purpose: only the footer write is guarded, so the
                # OSError it may raise is absorbed while the interrupt itself
                # keeps unwinding below.
                _write_failure_footer(
                    stdout, "spawn-harness interrupted", stderr_tail, output
                )
            if isinstance(exc, KeyboardInterrupt):
                raise SystemExit(128 + signal.SIGINT) from None
            raise
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
        if timed_out:
            _kill_process_group(proc)
            print(f"❌ Child timed out after {timeout:g}s", file=sys.stderr)
        pump.join(timeout=5)
        if timed_out:
            if stdout:
                _write_failure_footer(
                    stdout, f"timed out after {timeout:g}s", stderr_tail, output
                )
            return TIMEOUT_EXIT_CODE
        code = code if code >= 0 else 128 - code
        if stdout:
            if code != 0:
                _write_failure_footer(stdout, f"exit code {code}", stderr_tail, output)
            else:
                # Close our write handle before the degenerate check re-reads
                # and appends through a second handle — the child's own writes
                # went straight to the fd, but interleaving two handles on one
                # file is a trap not worth leaving open.
                _close_output(stdout, output)
                stdout = None
                warn_degenerate_output(agent_name, output)
        return code
    finally:
        if stdout:
            _close_output(stdout, output)


def _list_agents(config: Dict[str, dict]) -> None:
    if not config:
        print("No agents configured (launch with --agents <name,...>)")
        return
    for name in sorted(config):
        env = config[name].get("env", {})
        # Same selection path as a real spawn (parent = our environment),
        # so the listing never disagrees with what would actually run.
        harness = select_harness(
            build_child_env(dict(os.environ), env, {}), env, dict(os.environ)
        )
        model = env.get(LLM_NAME_ENV, "")
        summary = f"harness={harness.name}" + (f" model={model}" if model else "")
        if config[name].get("prompt", "").strip():
            summary += " prompt=preset"
        print(f"{name}  ({summary})")


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="spawn-harness",
        description=(
            "Run a launch-configured agent (--agents preset) as a "
            "non-interactive child harness process"
        ),
    )
    parser.add_argument(
        "agent", nargs="?", help="Agent name from LMER_SPAWN_AGENTS_CONFIG"
    )
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt", help="Prompt text for the child")
    prompt_group.add_argument("--prompt-file", help="File containing the prompt")
    parser.add_argument(
        "--output", help="Write the child's stdout to this file (default: our stdout)"
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Extra child environment (repeatable; overrides the agent's preset env)",
    )
    parser.add_argument(
        "--timeout", type=float, help="Kill the child after this many seconds (exit 124)"
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
        help=(
            "Seconds between 'still running' lines on stderr while the child "
            f"works (default {DEFAULT_HEARTBEAT_SECONDS:g}; 0 disables)"
        ),
    )
    parser.add_argument(
        "--list", action="store_true", help="List the configured agents and exit"
    )
    ns = parser.parse_args(argv)

    config = load_agents_config(os.environ)

    if ns.list:
        _list_agents(config)
        raise SystemExit(0)

    if not ns.agent:
        raise _fail("An agent name is required (or use --list)")

    agent = resolve_agent(ns.agent, config)
    prompt = resolve_prompt(ns, agent)
    extra_env = parse_env_pairs(ns.env)
    agent_env = agent.get("env", {})
    child_env = build_child_env(dict(os.environ), agent_env, extra_env)
    harness = select_harness(child_env, {**agent_env, **extra_env}, dict(os.environ))
    warn_missing_credentials(ns.agent, harness, dict(os.environ))
    apply_harness_extra_env(child_env, harness)
    prepend_user_harness_path(child_env, harness)

    try:
        child_argv, warnings = build_exec_argv(
            harness,
            prompt,
            model=child_env.get(LLM_NAME_ENV),
            effort=child_env.get("LMER_REASONING_EFFORT"),
            # Fan-out children run with no human attached — opt into the
            # harness's permission-bypass posture (container is the boundary).
            unattended=True,
        )
    except ValueError as exc:
        raise _fail(str(exc)) from None
    for warning in warnings:
        print(f"⚠️  {warning}", file=sys.stderr)

    # The prompt is elided — it can be huge and is the caller's own text.
    print(
        f"🚀 spawn-harness {ns.agent}: {' '.join(child_argv[:-1])} <prompt>",
        file=sys.stderr,
    )
    raise SystemExit(
        run_child(
            child_argv,
            child_env,
            ns.output,
            ns.timeout,
            heartbeat=ns.heartbeat,
            agent_name=ns.agent,
        )
    )


if __name__ == "__main__":
    main()
