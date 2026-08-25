"""
Agent harness registry.

A *harness* is the agent CLI that runs inside the session container (Claude
Code, Codex, pi, ...). This module is the single source of truth for
everything the host CLI and the in-container supervisor need to know about
each supported harness:

- which container runner script launches it (``libexec/<name>-runner.sh``)
- which host credential files to bind-mount into the container
- how the PTY supervisor should drive its TUI (ready marker, start-command
  injection, quit sequence)
- which Containerfile build-arg busts its install layer's cache

Selection: ``lmer --harness <name>`` wins over the ``LMER_HARNESS`` environment
variable; when neither is set the model name in ``LMER_LLM_NAME`` (exported, or
set by ``lmer --model``) can imply a harness (word-bounded match, e.g.
``sonnet`` → claude, ``gpt`` → codex — see :data:`MODEL_HARNESS_HINTS`), and the
final fallback is ``claude``. The
resolved name is forwarded into the container as ``LMER_HARNESS`` so
container-side code (``clone_and_exec.py``, ``lmer-supervisor``) agrees with
the host about which harness is running — container-side resolution therefore
never consults the model hint (see :func:`resolve_harness_name`).

Adding a new harness: see docs/HARNESSES.md for the full checklist. The short
version: add a :class:`Harness` entry to :data:`HARNESSES` here, add a
``libexec/<name>-runner.sh`` runner script, add an ``agent-files/<name>/``
config tree, install the CLI in the Containerfile behind a
``<NAME>_CACHE_BUST`` build-arg, and document the capability tier.

Users can also install harnesses without touching this registry: a drop-in
directory under ``~/.lmer/harnesses/`` carrying a declarative manifest and a
runner script (:mod:`lmer_cli.user_harnesses`, issue #132). :data:`HARNESSES`
stays built-ins-only; every lookup/resolution helper here consults the merged
view (:func:`known_harnesses`), and user definitions can never shadow a
built-in name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Tuple

#: Environment variable that selects the harness (container and host side).
HARNESS_ENV = "LMER_HARNESS"

#: Environment variable carrying the session's model name (passed verbatim to
#: the harness's model flag by the runner scripts). Also consulted for harness
#: autoselection when no harness is configured explicitly. Set either by export
#: or by ``lmer --model``, which wins over an exported value and over a preset's
#: env — the flag is applied to the environment before anything reads this, so
#: there is one answer here rather than a flag and a variable that can disagree.
LLM_NAME_ENV = "LMER_LLM_NAME"

#: Environment variable marking a session with no human attached (a
#: ``spawn-harness`` child, a cron run, any headless launch). The rule it
#: stands for is the NON-INTERACTIVE SESSIONS section of AGENTS.md: no gate
#: may end the turn with a question there, because nobody is going to answer
#: it. The variable is the *signal*, never the delivery — nothing renders an
#: environment value into a model's context, so the rule text travels
#: separately: ``prompts/non-interactive.md`` for full sessions (appended to
#: the system prompt by ``libexec/claude-runner.sh``, written to the global
#: context file by ``harness_render_global_context`` for codex/pi), and
#: :data:`~lmer_cli.container.spawn_harness.NONINTERACTIVE_NOTICE` in-band for
#: fan-out children, which run no runner script. Set unconditionally on those
#: children (see :func:`lmer_cli.container.spawn_harness.build_child_env`);
#: host-exported values are forwarded into the container for headless
#: launches. Truthy parsing (``1``/``true``/``yes``) is applied by the shell
#: readers, matching every other boolean ``LMER_*`` variable.
NONINTERACTIVE_ENV = "LMER_NONINTERACTIVE"

#: Word-bounded, case-insensitive model-name words that imply a harness, in
#: match order. Used only when neither ``--harness`` nor ``LMER_HARNESS`` is
#: set: a model family name is usually enough to pick the right CLI
#: (``LMER_LLM_NAME=gpt-5.2`` → codex) without configuring two variables.
MODEL_HARNESS_HINTS: Tuple[Tuple[str, str], ...] = (
    ("opus", "claude"),
    ("haiku", "claude"),
    ("fable", "claude"),
    ("sonnet", "claude"),
    ("mythos", "claude"),
    # gpt covers every current codex-served id (gpt-5.6-sol/terra/luna,
    # gpt-5.5, gpt-5.4[-mini], gpt-5.3-codex[-spark], ...); codex/o3/o4 catch
    # the ids without a gpt segment (codex-mini-latest, o3, o3-pro, o4-mini).
    ("gpt", "codex"),
    ("codex", "codex"),
    ("o3", "codex"),
    ("o4", "codex"),
)

#: The harness used when nothing is configured — existing installs see no
#: behavior change.
DEFAULT_HARNESS = "claude"

#: Reasoning-effort tiers accepted verbatim by every exec profile
#: (``max`` additionally maps per-profile via
#: :attr:`ExecProfile.effort_max_value`; ``auto``/unset emit no flag).
EXEC_EFFORT_TIERS = ("low", "medium", "high", "xhigh")

#: Start instruction injected for harnesses that have no native ``/start``
#: slash command. The text is typed into the TUI by the supervisor exactly like
#: a human prompt; the hook it references prints the task instructions.
GENERIC_START_COMMAND = (
    "Run `bash /Agents/global/hooks/start.sh` to load your task instructions, "
    "then follow them."
)


class UnknownHarnessError(ValueError):
    """Raised when a harness name is not in the registry (incl. user harnesses)."""

    def __init__(self, name: str):
        known = ", ".join(sorted(known_harnesses()))
        super().__init__(f"Unknown harness {name!r} (known harnesses: {known})")
        self.name = name


@dataclass(frozen=True)
class CredentialMount:
    """A host credential file to bind-mount into the container.

    ``host_path`` is relative to the host user's home directory;
    ``container_path`` is absolute inside the container. Only mounted when the
    host file exists (mirrors the long-standing claude credentials behavior).
    ``mode`` is ``rw`` (the historical default — token-refreshing credential
    files need it) or ``ro`` for hand-authored config the harness only reads.
    """

    host_path: str
    container_path: str
    mode: str = "rw"


@dataclass(frozen=True)
class SupervisorProfile:
    """How ``lmer-supervisor`` drives this harness's TUI over the PTY.

    ``ready_marker`` — byte sequence whose appearance in the output stream
    means the TUI is ready for input. Empty bytes disable marker gating (the
    supervisor falls back to its fixed delays). Overridable at runtime via
    ``LMER_AUTO_START_READY_MARKER``.

    ``start_command`` — text typed (and submitted) to begin the task.
    Claude has a native ``/start`` slash command; other harnesses get a plain
    instruction referencing the start hook. Overridable via
    ``LMER_START_COMMAND``.

    ``quit_sequence`` — byte payloads written one at a time (with the shutdown
    chord gap between writes) to make the TUI exit cleanly. Overridable via
    ``LMER_QUIT_SEQUENCE`` (steps separated by ``|``, each step
    unicode-escape decoded, e.g. ``\\x03|\\x03`` or ``/quit\\r``).

    ``ready_timeout`` — per-harness default for the marker wait (seconds);
    ``None`` keeps the supervisor's global default. Overridable via
    ``LMER_AUTO_START_READY_TIMEOUT``.
    """

    ready_marker: bytes
    start_command: str
    quit_sequence: Tuple[bytes, ...]
    ready_timeout: Optional[float] = None


@dataclass(frozen=True)
class ExecProfile:
    """How to run this harness as a non-interactive child process.

    Used by ``spawn-harness`` (``lmer_cli.container.spawn_harness``) to fan a
    task out to additional agents inside the session container.

    ``permission_bypass_args`` carries the harness's permission-bypass flags,
    deliberately segregated from the neutral ``base_args``: they encode a
    security-posture decision (an unattended child cannot answer interactive
    permission prompts; the lmer container is the security boundary — the
    same doctrine pi applies to interactive sessions), so
    :func:`build_exec_argv` appends them only when the caller passes
    ``unattended=True``. A future consumer of these profiles must opt into
    permission-free children knowingly rather than inherit the flags from
    generic registry data.

    ``model_args`` / ``effort_args`` are argv fragments with ``{model}`` /
    ``{effort}`` placeholders, appended only when a model/effort is supplied.
    ``effort_max_value`` is what the shared ``max`` tier maps to — harnesses
    whose top tier is ``xhigh`` alias it (mirrors ``harness_map_effort`` in
    ``libexec/harness-common.sh``); claude accepts ``max`` natively.

    The prompt is always the final positional argument, preceded by a ``--``
    sentinel when ``dashdash_before_prompt`` is set so prompt text starting
    with ``-`` cannot be parsed as flags by the child CLI; a harness whose
    parser does not honor ``--`` leaves it unset and
    :func:`build_exec_argv` rejects such prompts instead.
    """

    base_args: Tuple[str, ...]
    permission_bypass_args: Tuple[str, ...] = ()
    model_args: Tuple[str, ...] = ()
    effort_args: Tuple[str, ...] = ()
    effort_max_value: str = "max"
    dashdash_before_prompt: bool = False


@dataclass(frozen=True)
class Harness:
    """Registry entry describing one supported agent harness."""

    name: str
    #: Binary the runner script ultimately execs (informational; the runner
    #: script owns the real invocation).
    binary: str
    #: Command token passed through ``clone_and_exec.py`` to select the runner.
    #: Claude keeps the historical ``claude-runner`` token so a new host CLI
    #: remains compatible with older images.
    runner_command: str
    #: Runner script filename under ``libexec/``.
    runner_script: str
    #: Host credential files to bind-mount when present.
    credential_mounts: Tuple[CredentialMount, ...]
    supervisor: SupervisorProfile
    #: Containerfile build-arg that busts this harness's install layer.
    cache_bust_arg: str
    #: Non-interactive child-process invocation (``spawn-harness``).
    exec_profile: ExecProfile
    description: str = ""
    #: Extra fixed environment for the container when this harness is active.
    #: (e.g. disable self-updates inside ephemeral containers)
    extra_env: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    #: Absolute CONTAINER directory this harness writes its session JSONL
    #: under. Declared here so an orchestrator can mount each harness's
    #: transcripts out to the host instead of only claude's (issue #280):
    #: ``lmer`` runs the container ``--rm``, so an unmounted session directory
    #: dies with the session and the chat view has nothing to read back.
    #: ``None`` for a harness that writes no transcript worth keeping.
    session_dir: Optional[str] = None
    #: Absolute CONTAINER directory this harness keeps its agent memory in, so an
    #: orchestrator can mount it out of a ``--rm`` container (issue #325).
    #: ``None`` for a harness with no *native* memory feature.
    memory_dir: Optional[str] = None
    #: For user-installed harnesses (lmer_cli.user_harnesses): the host
    #: directory the definition was loaded from. ``None`` for built-ins —
    #: the "is this a user harness" discriminator.
    source_dir: Optional[str] = None
    #: Extra word-bounded model-name hints this harness contributes to
    #: autoselection, checked after :data:`MODEL_HARNESS_HINTS` (a user
    #: harness can never steal a built-in family name). Built-ins keep
    #: their hints in :data:`MODEL_HARNESS_HINTS` for match-order control.
    model_hints: Tuple[str, ...] = field(default_factory=tuple)


HARNESSES: dict[str, Harness] = {
    "claude": Harness(
        name="claude",
        binary="claude",
        runner_command="claude-runner",
        runner_script="claude-runner.sh",
        credential_mounts=(
            CredentialMount(".claude/.credentials.json", "/home/developer/.claude/.credentials.json"),
            CredentialMount(".claude.json", "/home/developer/.claude.json"),
        ),
        supervisor=SupervisorProfile(
            # "❯" (U+276F) — claude's input-prompt glyph.
            ready_marker=b"\xe2\x9d\xaf",
            start_command="/start",
            # Ctrl-C twice; the gap lets claude render its "press again to
            # exit" state so the second press confirms.
            quit_sequence=(b"\x03", b"\x03"),
        ),
        cache_bust_arg="CLAUDE_CACHE_BUST",
        exec_profile=ExecProfile(
            # -p prints the final response and exits; --no-session-persistence:
            # stateless children leave no session transcripts behind.
            base_args=("-p", "--no-session-persistence"),
            permission_bypass_args=("--dangerously-skip-permissions",),
            model_args=("--model", "{model}"),
            # claude's --effort accepts max natively — no aliasing needed.
            effort_args=("--effort", "{effort}"),
            # commander.js honors `--` (operands follow verbatim).
            dashdash_before_prompt=True,
        ),
        description="Claude Code (Anthropic) — full feature tier",
        # The platform's transcript mount destination since T22 — one fact with
        # lmer_platform.transcripts.CONTAINER_TRANSCRIPT_DIR, which
        # lmer_platform.spawn refuses to spawn without agreeing with.
        session_dir="/home/developer/.claude/projects",
        # Below session_dir by claude's layout, which is why it needs a mount of
        # its own: the platform binds a per-session transcript directory at the
        # parent. ``-workspace`` is claude's encoding of the container cwd, the
        # same encoding work_repo.memory relies on.
        memory_dir="/home/developer/.claude/projects/-workspace/memory",
    ),
    "codex": Harness(
        name="codex",
        binary="codex",
        runner_command="codex-runner",
        runner_script="codex-runner.sh",
        credential_mounts=(
            CredentialMount(".codex/auth.json", "/home/developer/.codex/auth.json"),
        ),
        supervisor=SupervisorProfile(
            # "›" (U+203A) — codex's composer prompt glyph.
            ready_marker=b"\xe2\x80\xba",
            start_command=GENERIC_START_COMMAND,
            # "/quit" + Enter is stable across codex versions; the Ctrl-C
            # semantics changed between releases (double-press → single-press).
            quit_sequence=(b"/quit\r",),
        ),
        cache_bust_arg="CODEX_CACHE_BUST",
        exec_profile=ExecProfile(
            # --ephemeral: stateless children leave no session files behind.
            base_args=("exec", "--skip-git-repo-check", "--ephemeral"),
            # codex's bwrap/seccomp sandbox cannot initialize under the
            # container's no-new-privileges (same finding as codex-runner.sh),
            # and an unattended child cannot answer approval prompts — the
            # bypass flag covers both.
            permission_bypass_args=("--dangerously-bypass-approvals-and-sandbox",),
            model_args=("--model", "{model}"),
            effort_args=("-c", "model_reasoning_effort={effort}"),
            # codex's top tier is xhigh.
            effort_max_value="xhigh",
            # clap honors `--` (everything after is the positional prompt).
            dashdash_before_prompt=True,
        ),
        description="Codex CLI (OpenAI) — core feature tier",
        # codex-cli 0.147.0: rollout files under a year/month/day tree in here.
        session_dir="/home/developer/.codex/sessions",
    ),
    "pi": Harness(
        name="pi",
        binary="pi",
        runner_command="pi-runner",
        runner_script="pi-runner.sh",
        credential_mounts=(
            # Written 0600 by pi's in-TUI /login; provider API keys can also
            # flow via .env / environment instead.
            CredentialMount(".pi/agent/auth.json", "/home/developer/.pi/agent/auth.json"),
            # pi's custom provider/model registry (may carry apiKey fields) —
            # how users register self-hosted endpoints, e.g. a local
            # llama.cpp server. Without it, in-container pi rejects
            # LMER_LLM_NAME values that only exist in the host's registry.
            # Hand-authored config pi only reads, hence ro.
            CredentialMount(
                ".pi/agent/models.json",
                "/home/developer/.pi/agent/models.json",
                mode="ro",
            ),
        ),
        supervisor=SupervisorProfile(
            # pi renders no prompt glyph; this startup-help line is the most
            # stable contiguous byte run at ready state (present with default
            # settings — do not set quietStartup in agent-files/pi).
            ready_marker=b"Press ctrl+o",
            start_command=GENERIC_START_COMMAND,
            # Ctrl-C twice — verified against pi 0.80.3 (first press clears
            # the editor, second exits cleanly).
            quit_sequence=(b"\x03", b"\x03"),
            # Node cold start can take ~20s in constrained containers.
            ready_timeout=60.0,
        ),
        cache_bust_arg="PI_CACHE_BUST",
        exec_profile=ExecProfile(
            # -p processes the prompt and exits; --no-session: stateless
            # children save no session.
            base_args=("-p", "--no-session"),
            # --no-approve also keeps the target repo's own .pi/ resources
            # unloaded (pi-runner's default posture).
            permission_bypass_args=("--no-approve",),
            model_args=("--model", "{model}"),
            # pi 0.80 lists a native `--thinking max`, but the interactive
            # runner (harness_map_effort) conservatively maps max→xhigh and
            # older pi versions lack the max tier — mirror that here so exec
            # and interactive sessions agree across image vintages.
            effort_args=("--thinking", "{effort}"),
            effort_max_value="xhigh",
            # pi's argv parser has no documented `--` handling; prompts that
            # could parse as flags are rejected by build_exec_argv instead.
        ),
        description="pi (earendil-works/pi, formerly badlogic/pi-mono) — core feature tier",
        extra_env=(("PI_SKIP_VERSION_CHECK", "1"),),
        # pi 0.84.1: one subdirectory per project, one JSONL per session.
        session_dir="/home/developer/.pi/agent/sessions",
    ),
}


def known_harnesses() -> dict[str, Harness]:
    """The full registry view: built-ins merged with user-installed harnesses.

    Built-ins win on collision (the user-harness loader already refuses to
    load a shadowing name, this is belt-and-braces). The user side comes
    from :mod:`lmer_cli.user_harnesses`, imported lazily — that module
    imports this one's dataclasses.
    """
    from .user_harnesses import load_user_harnesses

    return {**load_user_harnesses(), **HARNESSES}


def get_harness(name: str) -> Harness:
    """Return the registry entry for ``name`` (built-in or user-installed).

    Raises:
        UnknownHarnessError: if ``name`` is not a known harness.
    """
    try:
        return HARNESSES[name]
    except KeyError:
        pass
    entry = known_harnesses().get(name)
    if entry is None:
        raise UnknownHarnessError(name)
    return entry


def harness_for_model(model: Optional[str]) -> Optional[str]:
    """Return the harness name implied by a model name, or ``None``.

    Matching is word-bounded and case-insensitive (:data:`MODEL_HARNESS_HINTS`):
    ``anthropic/claude-sonnet-5`` matches ``sonnet``, ``gpt-5.2`` matches
    ``gpt``, but ``chatgpt-like`` matches nothing. User-installed harnesses'
    ``model_hints`` are checked after every built-in hint, in load order. An
    unrecognized model yields ``None`` — no error, since lmer never validates
    model names (the harness itself rejects unknown models).
    """
    if not model:
        return None
    user_hints = tuple(
        (word, harness.name)
        for harness in known_harnesses().values()
        if harness.source_dir is not None
        for word in harness.model_hints
    )
    for word, harness_name in (*MODEL_HARNESS_HINTS, *user_hints):
        if re.search(rf"\b{re.escape(word)}\b", model, re.IGNORECASE):
            return harness_name
    return None


def implied_harness_name(
    overlay_env: Mapping[str, str], merged_env: Mapping[str, str]
) -> str:
    """Return the harness name a fan-out child's environment selects.

    The single home of the fan-out harness-selection precedence, shared by
    the in-container resolver (``spawn-harness``'s ``select_harness``) and
    the host CLI's launch-time credential-mount computation (issue #131), so
    the two can never drift: ``LMER_HARNESS`` set by the agent itself
    (*overlay_env* — its preset env / ``--env`` pairs) > model hint from the
    agent's own ``LMER_LLM_NAME`` (*overlay_env* again) > the inherited
    ``LMER_HARNESS`` (*merged_env* — the overlay merged over the inherited
    environment) > default.

    The model hint deliberately reads the overlay, not the merged env: an
    agent that configures nothing runs the session's own harness. Hinting
    off the *inherited* ``LMER_LLM_NAME`` would let the session's model
    outrank the session's explicitly chosen harness — inverting the
    host-side flag-beats-hint precedence for e.g. a ``--harness pi`` session
    running an Anthropic model name via API keys.

    The returned name is stripped/lowercased but NOT validated — an unknown
    explicit name passes through so each caller can fail in its own way.
    """
    name = (overlay_env.get(HARNESS_ENV) or "").strip().lower()
    if not name:
        name = (
            harness_for_model(overlay_env.get(LLM_NAME_ENV))
            or (merged_env.get(HARNESS_ENV) or "").strip().lower()
            or DEFAULT_HARNESS
        )
    return name


def missing_credential_mounts(
    harness: "Harness", exists: Callable[["CredentialMount"], bool]
) -> Tuple["CredentialMount", ...]:
    """The warn-iff-ALL-credential-files-missing policy (issue #131).

    Single home of the policy shared by the launch-time (host) and
    spawn-time (container) may-fail-to-authenticate warnings, which differ
    only in *exists* (host checks ``~/<host_path>``, the container checks
    ``container_path``). Returns *harness*'s credential mounts whose files
    are missing — but only when NONE exist; a partial credential set
    returns empty (pi's ``models.json`` is optional config, not a gap
    worth warning about). Each caller formats its own paths for display.
    """
    missing = tuple(c for c in harness.credential_mounts if not exists(c))
    if missing and len(missing) == len(harness.credential_mounts):
        return missing
    return ()


def resolve_harness_selection(explicit: Optional[str] = None) -> Tuple[str, str]:
    """Resolve the harness for a new session, tracking where the choice came from.

    Precedence: ``--harness`` flag > ``LMER_HARNESS`` env > model hint from
    ``LMER_LLM_NAME`` (:func:`harness_for_model`) > default. Returns
    ``(name, source)`` with ``source`` one of ``"flag"``, ``"env"``,
    ``"model"``, ``"default"`` so the CLI can announce an autoselection.

    Whitespace is stripped and the name lowercased so ``LMER_HARNESS=Codex``
    works. An unknown name raises :class:`UnknownHarnessError` — a
    misconfigured harness must fail loudly, not silently run claude.
    """
    raw = explicit if explicit is not None else os.environ.get(HARNESS_ENV, "")
    name = (raw or "").strip().lower()
    if name:
        source = "flag" if explicit is not None else "env"
    else:
        hinted = harness_for_model(os.environ.get(LLM_NAME_ENV, ""))
        if hinted is not None:
            name, source = hinted, "model"
        else:
            name, source = DEFAULT_HARNESS, "default"
    if name not in known_harnesses():
        raise UnknownHarnessError(name)
    return name, source


def resolve_harness_name(explicit: Optional[str] = None) -> str:
    """Resolve the active harness name: flag > ``LMER_HARNESS`` env > default.

    Deliberately does NOT consult the ``LMER_LLM_NAME`` model hint: this is
    the container-side resolver (``lmer-supervisor``), and the host always
    forwards the resolved harness as ``LMER_HARNESS`` — guessing from the
    model name here could mismatch the runner actually launched (e.g. an old
    host that only knows claude). Host-side session selection goes through
    :func:`resolve_harness_selection` instead.

    Whitespace is stripped and the name lowercased so ``LMER_HARNESS=Codex``
    works. An unknown name raises :class:`UnknownHarnessError` — a
    misconfigured harness must fail loudly, not silently run claude.
    """
    raw = explicit if explicit is not None else os.environ.get(HARNESS_ENV, "")
    name = (raw or "").strip().lower() or DEFAULT_HARNESS
    if name not in known_harnesses():
        raise UnknownHarnessError(name)
    return name


def resolve_harness(explicit: Optional[str] = None) -> Harness:
    """Resolve and return the active :class:`Harness` (see :func:`resolve_harness_name`)."""
    return get_harness(resolve_harness_name(explicit))


def map_exec_effort(
    harness: Harness, effort: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Map a shared reasoning-effort tier to this harness's exec value.

    Mirrors ``harness_map_effort`` in ``libexec/harness-common.sh``:
    ``max`` → the profile's :attr:`ExecProfile.effort_max_value`, the
    :data:`EXEC_EFFORT_TIERS` pass through, ``auto``/unset/empty yield no
    value, anything else is skipped with a warning. Returns
    ``(value_or_none, warning_or_none)``.
    """
    tier = (effort or "").strip().lower()
    if tier in ("", "auto"):
        return None, None
    if tier == "max":
        return harness.exec_profile.effort_max_value, None
    if tier in EXEC_EFFORT_TIERS:
        return tier, None
    return None, (
        f"Ignoring reasoning effort {effort!r} "
        "(expected: low|medium|high|xhigh|max|auto)"
    )


def build_exec_argv(
    harness: Harness,
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    unattended: bool = False,
) -> Tuple[list, list]:
    """Build the argv for a non-interactive run of ``harness``.

    Assembles ``binary + base_args [+ permission_bypass_args] [+ model_args]
    [+ effort_args] [+ --] + prompt`` from the harness's
    :class:`ExecProfile`. Returns ``(argv, warnings)`` — an unknown effort
    tier becomes a warning rather than an error, matching the interactive
    runners.

    ``unattended=True`` opts into the profile's
    :attr:`ExecProfile.permission_bypass_args` — the caller's explicit
    statement that no human can answer the child's permission prompts
    (``spawn-harness`` fan-out children). The default leaves the harness's
    permission checks intact so a future consumer cannot inherit the bypass
    posture silently.

    Raises:
        ValueError: when the prompt could be parsed as flags by the child
            (starts with ``-``) and the harness's parser has no ``--``
            sentinel to protect it.
    """
    profile = harness.exec_profile
    argv = [harness.binary, *profile.base_args]
    if unattended:
        argv += profile.permission_bypass_args
    warnings = []
    if model:
        if profile.model_args:
            argv += [arg.format(model=model) for arg in profile.model_args]
        else:
            # A profile with no model_args carries no CLI flag for the model.
            # This is EXPECTED for env-configured harnesses (the documented
            # wrapper pattern: the binary/wrapper reads LMER_LLM_NAME from the
            # child env), so the note is informational — it names the model
            # not added to argv rather than claiming it was lost, since a
            # correctly-wired wrapper still delivers it via the environment.
            warnings.append(
                f"{harness.name}: model {model!r} not added to the child argv "
                f"(exec profile defines no model flag). Expected for an "
                f"env-configured harness whose binary/wrapper reads "
                f"LMER_LLM_NAME; otherwise the child runs its default model."
            )
    effort_value, warning = map_exec_effort(harness, effort)
    if warning:
        warnings.append(warning)
    if effort_value:
        if profile.effort_args:
            argv += [arg.format(effort=effort_value) for arg in profile.effort_args]
        else:
            warnings.append(
                f"{harness.name}: reasoning effort {effort_value!r} not added "
                f"to the child argv (exec profile defines no effort flag). "
                f"Expected for an env-configured harness reading "
                f"LMER_REASONING_EFFORT; otherwise the child uses its default."
            )
    if profile.dashdash_before_prompt:
        argv.append("--")
    elif prompt.lstrip().startswith("-"):
        raise ValueError(
            f"Prompt starts with '-' and the {harness.name} CLI has no '--' "
            "sentinel — the child would parse it as flags. Reword the prompt "
            "(e.g. open with a sentence, not a dash)."
        )
    argv.append(prompt)
    return argv, warnings
