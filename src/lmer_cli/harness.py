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
variable; when neither is set the model name in ``LMER_LLM_NAME`` can imply a
harness (word-bounded match, e.g. ``sonnet`` → claude, ``gpt`` → codex — see
:data:`MODEL_HARNESS_HINTS`), and the final fallback is ``claude``. The
resolved name is forwarded into the container as ``LMER_HARNESS`` so
container-side code (``clone_and_exec.py``, ``lmer-supervisor``) agrees with
the host about which harness is running — container-side resolution therefore
never consults the model hint (see :func:`resolve_harness_name`).

Adding a new harness: see docs/HARNESSES.md for the full checklist. The short
version: add a :class:`Harness` entry to :data:`HARNESSES` here, add a
``libexec/<name>-runner.sh`` runner script, add an ``agent-files/<name>/``
config tree, install the CLI in the Containerfile behind a
``<NAME>_CACHE_BUST`` build-arg, and document the capability tier.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple

#: Environment variable that selects the harness (container and host side).
HARNESS_ENV = "LMER_HARNESS"

#: Environment variable carrying the session's model name (passed verbatim to
#: the harness's model flag by the runner scripts). Also consulted for harness
#: autoselection when no harness is configured explicitly.
LLM_NAME_ENV = "LMER_LLM_NAME"

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

#: Start instruction injected for harnesses that have no native ``/start``
#: slash command. The text is typed into the TUI by the supervisor exactly like
#: a human prompt; the hook it references prints the task instructions.
GENERIC_START_COMMAND = (
    "Run `bash /Agents/global/hooks/start.sh` to load your task instructions, "
    "then follow them."
)


class UnknownHarnessError(ValueError):
    """Raised when a harness name is not in the registry."""

    def __init__(self, name: str):
        known = ", ".join(sorted(HARNESSES))
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
    description: str = ""
    #: Extra fixed environment for the container when this harness is active.
    #: (e.g. disable self-updates inside ephemeral containers)
    extra_env: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)


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
        description="Claude Code (Anthropic) — full feature tier",
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
        description="Codex CLI (OpenAI) — core feature tier",
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
        description="pi (earendil-works/pi, formerly badlogic/pi-mono) — core feature tier",
        extra_env=(("PI_SKIP_VERSION_CHECK", "1"),),
    ),
}


def get_harness(name: str) -> Harness:
    """Return the registry entry for ``name``.

    Raises:
        UnknownHarnessError: if ``name`` is not a known harness.
    """
    try:
        return HARNESSES[name]
    except KeyError:
        raise UnknownHarnessError(name) from None


def harness_for_model(model: Optional[str]) -> Optional[str]:
    """Return the harness name implied by a model name, or ``None``.

    Matching is word-bounded and case-insensitive (:data:`MODEL_HARNESS_HINTS`):
    ``anthropic/claude-sonnet-5`` matches ``sonnet``, ``gpt-5.2`` matches
    ``gpt``, but ``chatgpt-like`` matches nothing. An unrecognized model yields
    ``None`` — no error, since lmer never validates model names (the harness
    itself rejects unknown models).
    """
    if not model:
        return None
    for word, harness_name in MODEL_HARNESS_HINTS:
        if re.search(rf"\b{re.escape(word)}\b", model, re.IGNORECASE):
            return harness_name
    return None


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
    if name not in HARNESSES:
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
    if name not in HARNESSES:
        raise UnknownHarnessError(name)
    return name


def resolve_harness(explicit: Optional[str] = None) -> Harness:
    """Resolve and return the active :class:`Harness` (see :func:`resolve_harness_name`)."""
    return get_harness(resolve_harness_name(explicit))
