"""spawn-harness — run a launch-configured agent as a child harness process.

The in-container half of the ``--agents`` fan-out (issue #130): the host CLI
resolves ``--agents <name,...>`` against the operator's presets file and
forwards only the resolved per-agent config into the container as
``LMER_AGENTS_CONFIG`` (JSON ``{name: {"env": {...}, "prompt": "..."?}}`` —
the env overlay plus an optional prompt preamble folded from the preset's
args; names also listed in ``LMER_AGENTS``). This tool lets the
orchestrating agent run one of those agents non-interactively::

    spawn-harness sol-review --prompt-file prompt.md \\
        --env LMER_REVIEW_ON_MR=0 --output agents/sol-review.md

One invocation runs one child and blocks until it exits; the orchestrating
agent parallelizes with its own background-shell tooling. Children are
stateless — no run dirs, no work-repo writes, no supervisor — and run with
their harness's permission checks bypassed (the lmer container is the
security boundary; see :class:`lmer_cli.harness.ExecProfile`).

Child environment: parent env, overlaid with the agent's preset ``env``,
overlaid with ``--env KEY=VAL`` pairs (last wins). ``LMER_AGENTS`` /
``LMER_AGENTS_CONFIG`` are stripped from the child so children cannot fan
out further (no grandchildren, structurally). The agent's own overlay
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
    HARNESSES,
    LLM_NAME_ENV,
    Harness,
    build_exec_argv,
    harness_for_model,
    implied_harness_name,
    missing_credential_mounts,
)

# Both halves of the fan-out env contract live in lmer_cli.presets (the
# host-side writer) so the writer and this consumer can never drift apart.
from lmer_cli.presets import AGENTS_CONFIG_ENV, AGENTS_ENV

#: Exit code for a child killed by ``--timeout`` (the coreutils convention).
TIMEOUT_EXIT_CODE = 124

#: Default seconds between "still running" heartbeat lines on stderr.
DEFAULT_HEARTBEAT_SECONDS = 60.0

#: How many trailing stderr lines the failure footer preserves.
STDERR_TAIL_LINES = 40


def _fail(message: str) -> "SystemExit":
    print(f"❌ {message}", file=sys.stderr)
    return SystemExit(2)


def load_agents_config(environ: Dict[str, str]) -> Dict[str, dict]:
    """Parse ``LMER_AGENTS_CONFIG`` from ``environ``.

    Returns the ``{name: {"env": {...}}}`` mapping. Absent/empty means no
    agents were configured at launch. A malformed value is a hard error —
    the variable is machine-written by the host CLI, so damage means the
    session is misconfigured, not that the user typo'd.
    """
    raw = environ.get(AGENTS_CONFIG_ENV, "").strip()
    if not raw:
        return {}
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _fail(f"{AGENTS_CONFIG_ENV} is not valid JSON: {exc}") from None
    if not isinstance(config, dict):
        raise _fail(f"{AGENTS_CONFIG_ENV} must be a JSON object, got {type(config).__name__}")
    for name, entry in config.items():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("env", {}), dict)
            or not isinstance(entry.get("prompt", ""), str)
        ):
            raise _fail(f"{AGENTS_CONFIG_ENV} entry {name!r} is malformed")
    return config


def resolve_agent(name: str, config: Dict[str, dict]) -> dict:
    """Return the config entry for ``name``, failing loudly when unknown."""
    if not config:
        raise _fail(
            "No agents configured — launch the session with "
            "--agents <name,...> (or LMER_AGENTS) to enable spawn-harness"
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
    children — the no-grandchildren rule is structural, not advisory.
    """
    child = dict(parent_env)
    child.update(agent_env)
    child.update(extra_env)
    child.pop(AGENTS_ENV, None)
    child.pop(AGENTS_CONFIG_ENV, None)
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
    if name not in HARNESSES:
        known = ", ".join(sorted(HARNESSES))
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
    return HARNESSES[name]


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
    """
    preamble = agent.get("prompt", "")
    if ns.prompt is None and ns.prompt_file is None:
        if not preamble.strip():
            raise _fail("A prompt is required: --prompt or --prompt-file")
        return preamble
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
        return f"{preamble}\n\n{prompt}"
    return prompt


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
) -> int:
    """Run the child process, capturing stdout to ``output`` when given.

    Without ``--output`` the child's stdout flows to ours; stderr always
    passes through for diagnosis (and its tail is appended to the output
    file as a failure footer when the child dies, so a failed agent's
    output file explains itself). While the child runs, a heartbeat line is
    printed to stderr every ``heartbeat`` seconds (0 disables) — harnesses
    buffer their final answer, so the output file staying empty says
    nothing about liveness. Returns the child's exit code — a signal-killed
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
    configuration problem.
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
                _failure_footer(stdout, "spawn-harness interrupted", stderr_tail)
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
                _failure_footer(stdout, f"timed out after {timeout:g}s", stderr_tail)
            return TIMEOUT_EXIT_CODE
        code = code if code >= 0 else 128 - code
        if code != 0 and stdout:
            _failure_footer(stdout, f"exit code {code}", stderr_tail)
        return code
    finally:
        if stdout:
            stdout.close()


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
    parser.add_argument("agent", nargs="?", help="Agent name from LMER_AGENTS_CONFIG")
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
        run_child(child_argv, child_env, ns.output, ns.timeout, heartbeat=ns.heartbeat)
    )


if __name__ == "__main__":
    main()
