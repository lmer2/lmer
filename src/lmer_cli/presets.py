"""Named startup presets for spawned ``lmer`` sessions.

A *preset* is an operator-defined, named startup configuration - a local
checkout to mount, a running service container to target (service mode), extra
environment variables, and extra ``lmer`` CLI flags - that a spawner can layer
onto an ``lmer`` invocation by name. Presets live in core lmer so they are not
tied to any single spawner; current consumers are the host-side Slack listener
(:mod:`slack_chat.listener`), which spawns a repo-less ``lmer chat`` session
per mention/DM, and the ``lmer`` CLI itself (``--preset <name>`` /
``LMER_PRESET``, issue #127).

This is deliberately **not** a raw passthrough of ``--service`` / ``--checkout``.
A caller (e.g. a Slack user) only *selects* one of the operator-defined presets
by name, never specifying a host path or container directly. The trust boundary
is the presets file itself, which lives on the host and is writable only by
whoever runs the spawner. Access to *use* presets is therefore the same as
access to reach the spawner (for the Slack listener: channel membership / the
DM allowlist); the presets feature adds no separate gate.

Presets are defined in a JSON file pointed at by ``LMER_PRESETS_FILE``::

    {
      "my_service": {
        "checkout": "/srv/my-service",
        "service": "mysvc",
        "env": {"LMER_LLM_NAME": "opus"},
        "args": ["--ports", "2"]
      }
    }

A preset is selected by name: via a ``$preset:<name>`` token embedded in a
message, e.g. ``Hey @lmer $preset:my_service please do X`` (the Slack listener
scans the triggering message for it), or via ``lmer --preset <name>`` /
``LMER_PRESET=<name>`` on a direct CLI invocation (or, scoped to one taskdef,
``LMER_<TASK>_PRESET=<name>`` — e.g. ``LMER_REVIEW_PRESET`` applies to
``lmer review`` only, issue #140). Preset names must use the
selector charset (``[A-Za-z0-9_-]``); a name with other characters is logged
and skipped at load time, since the token could never select it.

All fields are optional, with one rule mirrored from the lmer CLI: a preset
that sets ``service`` must also set ``checkout`` (``--service`` requires
``--checkout``). Loading degrades gracefully - a missing/unreadable/malformed
file yields no presets, and a single invalid entry is logged and skipped so it
cannot disable the others. A caller that then selects a preset that did not
load gets the consumer's normal "unknown preset" rejection.

The user-facing guide — file format, trust model, and the per-consumer merge
semantics — is ``docs/PRESETS.md``.
"""

import json
import logging
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .harness import HARNESS_ENV, LLM_NAME_ENV, harness_for_model, known_harnesses

logger = logging.getLogger("lmer_cli.presets")

# Env var naming the JSON presets file. Read host-side only (by the Slack
# listener and the lmer CLI); it never needs to reach inside a container (the
# preset's effects do, as the --checkout/--service flags and env vars the
# spawned lmer already forwards).
PRESETS_FILE_ENV = "LMER_PRESETS_FILE"

# Env var selecting a preset by name for a CLI invocation (the --preset flag
# wins over it, matching --harness/LMER_HARNESS). Read host-side only, before
# the container starts; also honored from .env files, so a project directory
# can pin a default preset. The Slack listener selects via the $preset:<name>
# message token instead and never reads this.
PRESET_ENV = "LMER_PRESET"

# Template for the taskdef-scoped variant of PRESET_ENV (issue #140):
# LMER_REVIEW_PRESET selects the preset for `lmer review …` only, so an
# operator can pin a per-task default in ~/.lmer/.env without it bleeding
# into every other task. Read host-side alongside PRESET_ENV.
TASK_PRESET_ENV_TEMPLATE = "LMER_{task}_PRESET"

# Characters a taskdef id may contribute to an env-var name; everything else
# (dashes, dots) becomes an underscore so `code-review` can be selected as
# LMER_CODE_REVIEW_PRESET.
_ENV_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9]")

# Env var selecting the agent fan-out presets for a CLI invocation, as a
# comma-delimited list of preset names (the --agents flag wins over it,
# matching --preset/LMER_PRESET). Read host-side only: the names are resolved
# against the presets file before the container starts, and only the resolved
# env overlays are forwarded inside (under the scoped SPAWN_* names below,
# for spawn-harness) — the presets file itself never crosses the boundary.
AGENTS_ENV = "LMER_AGENTS"

# The config half of the same host-side input convention: never written by
# lmer, never read as input, and defined beside AGENTS_ENV because both
# spellings must be stripped from a fan-out child (a child carrying either
# one would hand the session's selection to whatever it runs). Every
# consumer imports them from here so a rename can't split them.
AGENTS_CONFIG_ENV = "LMER_AGENTS_CONFIG"

# Container-side names the resolved selection is actually forwarded under
# (issue #283), read by spawn-harness and by nothing host-side. Scoped away
# from AGENTS_ENV because inside the container the pair is ambient: forwarding
# the selection under the host *input* name made every nested `lmer` invocation
# and every test run in the session inherit it, and resolve those names against
# a presets file that never crosses the boundary. The input convention stays
# AGENTS_ENV; only what the container sees is renamed.
SPAWN_AGENTS_ENV = "LMER_SPAWN_AGENTS"
SPAWN_AGENTS_CONFIG_ENV = "LMER_SPAWN_AGENTS_CONFIG"

# Charset a preset name may use. Shared between the selector token and the
# load-time name check so a name that loads is always one the token can select.
_NAME_PATTERN = r"[A-Za-z0-9_-]+"

# Selector token: ``$preset:<name>`` anywhere in a triggering message. The name
# is restricted to word chars and dashes so the token ends cleanly at the first
# space or punctuation, and the ``$preset:`` prefix is distinctive enough not
# to fire by accident.
_PRESET_TOKEN_RE = re.compile(r"\$preset:(" + _NAME_PATTERN + r")")

# Full-string match used to reject preset names a ``$preset:`` token could
# never express (e.g. ``prod.api`` or ``my service``).
_VALID_NAME_RE = re.compile(_NAME_PATTERN)

# Keys a preset entry may define. Anything else is an (logged) typo signal.
_KNOWN_KEYS = frozenset({"checkout", "service", "env", "args"})


@dataclass
class Preset:
    """A named startup configuration for an lmer session.

    Attributes:
        name: The preset's key in the presets file (what ``$preset:<name>``
            or ``--preset <name>`` / ``LMER_PRESET`` selects).
        checkout: Host path to a local source checkout, passed as
            ``--checkout``. Required whenever ``service`` is set.
        service: Docker/Compose service or container name to target, passed
            as ``--service`` (service mode).
        env: Extra environment variables. How they merge is the consumer's
            contract: the Slack listener applies them over the spawned
            process's inherited environment (the preset wins on conflict),
            while a direct CLI invocation applies them as defaults (exported
            environment variables win; ``.env``-file values lose). Use for
            "other startup variables" such as ``LMER_LLM_NAME`` or
            ``LMER_REASONING_EFFORT``.
        args: Extra CLI tokens (e.g. ``["--ports", "2"]``). The Slack
            listener appends them verbatim to the spawned ``lmer chat``
            command; a direct CLI invocation applies them as overridable
            defaults and requires them to be flag tokens only.
    """

    name: str
    checkout: str | None = None
    service: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)

    def cli_tokens(self) -> list[str]:
        """The lmer CLI tokens this preset contributes.

        The single home of the field→flag mapping (``checkout`` →
        ``--checkout``, ``service`` → ``--service``, then ``args`` verbatim),
        shared by every spawner so a future preset field only needs wiring
        here.
        """
        tokens: list[str] = []
        if self.checkout:
            tokens += ["--checkout", self.checkout]
        if self.service:
            tokens += ["--service", self.service]
        return tokens + list(self.args)


def parse_preset_token(text: str | None) -> str | None:
    """Return the preset name selected in *text*, or ``None``.

    Recognizes the first ``$preset:<name>`` token in the message, where
    ``<name>`` is ``[A-Za-z0-9_-]+``. Returns ``None`` when *text* is empty or
    carries no token. Resolving the name against the configured presets (and
    rejecting unknown ones) is the caller's job.
    """
    if not text:
        return None
    match = _PRESET_TOKEN_RE.search(text)
    return match.group(1) if match else None


def task_preset_env_name(task_id: str | None) -> str | None:
    """Return the taskdef-scoped preset env var name for *task_id*.

    The single home of the taskdef-id → env-var mapping (issue #140):
    ``review`` → ``LMER_REVIEW_PRESET``, ``code-review`` →
    ``LMER_CODE_REVIEW_PRESET``. Taskdef ids are directory names from the
    taskdef search path (including work-repo and ``LMER_TASKDEF_PATHS``
    ones), so the derivation cannot assume the built-in set: everything
    outside ``[A-Za-z0-9]`` collapses to an underscore.

    The mapping is therefore **many-to-one**: ``code-review``,
    ``code_review`` and ``code.review`` all derive
    ``LMER_CODE_REVIEW_PRESET``, so two separator-variant taskdefs
    coexisting on the search path would share one selector (no current
    taskdef set does; there is nothing to disambiguate against here, since
    this function deliberately knows nothing about which taskdefs exist).

    Returns ``None`` when *task_id* is empty or normalizes to nothing —
    an id of only separators (``"-"``), or one with no ASCII alphanumerics
    at all (``"レビュー"``) — leaving that taskdef with no taskdef-scoped
    selector; ``LMER_PRESET`` still works for it. A digit can safely lead
    the suffix: the ``LMER_`` prefix keeps the full name a legal env-var
    identifier (``2fa`` → ``LMER_2FA_PRESET``).
    """
    if not task_id or not task_id.strip():
        return None
    suffix = _ENV_NAME_SAFE_RE.sub("_", task_id.strip()).strip("_").upper()
    if not suffix:
        return None
    return TASK_PRESET_ENV_TEMPLATE.format(task=suffix)


def preset_env_value(
    var: str, environ: Mapping[str, str] | None = None
) -> str:
    """Read a preset selector variable under the blank-is-unset rule.

    The one home for that normalization: the value is stripped, and a blank
    one (empty or whitespace-only) reads as ``""`` — i.e. unset. Shared by
    :func:`select_preset_name` and the CLI's tier-override warning so the two
    can never disagree about whether a given value selects anything.

    Args:
        var: Environment variable name to read.
        environ: Environment mapping; defaults to ``os.environ``.
    """
    env = os.environ if environ is None else environ
    return (env.get(var) or "").strip()


def preset_selector_vars(task_id: str | None) -> list[str]:
    """Return every env var that can select a preset for ``lmer <task_id>``.

    Ordered most specific first — ``LMER_<TASK>_PRESET`` then
    ``LMER_PRESET`` — matching the precedence :func:`select_preset_name`
    applies. The taskdef-scoped var is omitted when *task_id* has no
    derivable name (see :func:`task_preset_env_name`).

    This is the single home of the candidate list, shared by the selector
    itself and by spawners that need to *unset* the selectors in a child's
    environment: the Slack listener blanks them when a ``$preset:`` token
    selected a preset, so the token displaces the listener-wide default whole
    rather than stacking with it (issue #181). Keeping both callers on one
    list is what stops "what selects" and "what gets unset" from drifting
    apart when a future selector is added.
    """
    task_env = task_preset_env_name(task_id)
    return ([task_env] if task_env else []) + [PRESET_ENV]


def select_preset_name(
    flag: str | None,
    task_id: str | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve which preset a CLI invocation selects, and via what.

    Precedence (issue #140), most specific first:

    1. ``--preset`` — the explicit flag always wins.
    2. ``LMER_<TASK>_PRESET`` — the taskdef-scoped var for *task_id*, so a
       per-task default in ``~/.lmer/.env`` applies to that task only.
    3. ``LMER_PRESET`` — the generic default for every other task.

    Args:
        flag: The ``--preset`` value, or ``None``.
        task_id: The selected taskdef id, or ``None`` (``--no-task``), in
            which case only the flag and ``LMER_PRESET`` can select.
        environ: Environment mapping to read; defaults to ``os.environ``.

    Every candidate is whitespace-stripped, and a blank one (empty or
    whitespace-only) counts as **unset**: it falls through to the next
    candidate instead of selecting a preset named ``""``. So
    ``LMER_REVIEW_PRESET= lmer review …`` drops back to ``LMER_PRESET``, and
    ``LMER_PRESET= lmer develop …`` runs with no preset. Stripping applies to
    the flag too, so ``--preset " demo "`` and ``LMER_PRESET=" demo "`` agree
    (before this was asymmetric: only env values were stripped).

    Returns:
        ``(name, source)`` — the selected preset name and a human-readable
        label for what selected it (``"--preset"`` or the env var's name),
        for the announce line and the unknown-name error. ``(None, None)``
        when nothing selects a preset.
    """
    stripped_flag = (flag or "").strip()
    if stripped_flag:
        return stripped_flag, "--preset"
    for var in preset_selector_vars(task_id):
        value = preset_env_value(var, environ)
        if value:
            return value, var
    return None, None


def load_presets(path: str | None = None) -> dict[str, Preset]:
    """Load and validate presets from a JSON file.

    Args:
        path: Path to the presets file. Defaults to ``$LMER_PRESETS_FILE``.
            When unset/empty the feature is off and an empty mapping is
            returned.

    Returns:
        Mapping of preset name to :class:`Preset`. Empty when the feature is
        off or the file could not be loaded. Individual invalid entries are
        logged and skipped rather than aborting the whole load.
    """
    raw_path = path if path is not None else os.getenv(PRESETS_FILE_ENV, "")
    if not raw_path or not raw_path.strip():
        return {}

    file_path = Path(raw_path).expanduser()
    if not file_path.is_file():
        logger.error("presets_file_not_found path=%s", file_path)
        return {}

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("presets_file_unreadable path=%s error=%s", file_path, exc)
        return {}

    if not isinstance(data, dict):
        logger.error(
            "presets_file_not_object path=%s type=%s",
            file_path,
            type(data).__name__,
        )
        return {}

    presets: dict[str, Preset] = {}
    for name, spec in data.items():
        preset = _build_preset(str(name), spec)
        if preset is not None:
            presets[preset.name] = preset

    logger.info(
        "presets_loaded path=%s count=%s names=%s",
        file_path,
        len(presets),
        ",".join(sorted(presets)) or "(none)",
    )
    return presets


def _extract_arg_flag(args: list[str], flag: str) -> tuple[str | None, list[str]]:
    """Pull ``flag <value>`` / ``flag=<value>`` out of a preset args list.

    Returns ``(value, remaining_tokens)``; the last occurrence wins
    (argparse semantics). A trailing flag with no value is left in the
    remaining tokens for the caller's ignored-args warning.
    """
    value: str | None = None
    remaining: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == flag and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        if token.startswith(flag + "="):
            value = token.split("=", 1)[1]
            i += 1
            continue
        remaining.append(token)
        i += 1
    return value, remaining


def resolve_agent_presets(
    selection: str, presets: dict[str, Preset]
) -> tuple[dict[str, dict] | None, list[str], str | None]:
    """Resolve a comma-delimited ``--agents``/``LMER_AGENTS`` selection.

    For the fan-out consumer a preset is an agent configuration, not a
    session launch: the children are in-container subprocesses. What a
    selected preset contributes:

    - ``env`` — forwarded as the child's overlay.
    - ``args`` ``--harness <name>`` — folded into the overlay as
      ``LMER_HARNESS`` (winning over a preset-env value, mirroring the CLI
      consumer's flag-beats-env precedence), so a dual-use preset
      configures its harness once and works with both ``--preset`` and
      ``--agents``.
    - ``args`` ``--prompt <text>`` — carried as the agent's ``prompt``
      preamble; ``spawn-harness`` prepends it to the orchestrator-supplied
      prompt (or uses it alone when none is given).
    - everything else (``checkout``/``service``, remaining args) is
      surfaced as a warning and ignored.

    A name that matches no preset falls back to the **model route**: when
    the name is a model whose family implies a harness
    (:func:`harness_for_model` — e.g. ``fable`` → claude), it resolves to a
    synthesized ``{"env": {"LMER_LLM_NAME": <name>}}`` agent, so common
    model names need no preset entry (``--agents=fable,sol-review``). The
    fallback is surfaced as a note-level warning — a typo'd preset name
    that happens to contain a model word resolves this way and only fails
    at spawn time when the harness rejects the model.

    Duplicate names warn and keep the first occurrence; selection order is
    preserved.

    Returns ``(resolved, warnings, error)``: ``resolved`` maps name → the
    agent's config entry (``{"env": {...}}`` plus optional ``"prompt"`` —
    the exact ``LMER_SPAWN_AGENTS_CONFIG`` shape) and is ``None`` when ``error``
    is set (an empty selection, any unknown name — unknown must fail the
    invocation, mirroring the ``--preset`` UX, never silently spawn fewer
    agents — or an unknown harness, which would otherwise only surface
    hours later when spawn-harness runs inside the session).
    """
    names = [name.strip() for name in selection.split(",") if name.strip()]
    warnings: list[str] = []
    if not names:
        return None, warnings, "no agent names given"
    resolved: dict[str, dict] = {}
    for name in names:
        if name in resolved:
            warnings.append(f"duplicate agent '{name}' ignored")
            continue
        preset = presets.get(name)
        if preset is None:
            # A case-variant of a defined preset must not silently take the
            # model route (preset lookup is exact-case, the model hint is
            # case-insensitive — '--agents=Fable' with a 'fable' preset
            # would otherwise drop the preset's env without a sound).
            case_match = next(
                (key for key in presets if key.lower() == name.lower()), None
            )
            if case_match is not None:
                return None, warnings, (
                    f"Unknown agent '{name}' — did you mean '{case_match}'? "
                    "(preset names are case-sensitive)"
                )
            hinted = harness_for_model(name)
            if hinted is not None:
                warnings.append(
                    f"agent '{name}': no matching preset — "
                    f"using the model route ({name} → {hinted})"
                )
                resolved[name] = {"env": {LLM_NAME_ENV: name}}
                continue
            available = ", ".join(sorted(presets)) or "(none)"
            return None, warnings, (
                f"Unknown agent '{name}': not a preset (available: {available}) "
                "and not a model name that routes to a harness"
            )
        harness_arg, leftover_args = _extract_arg_flag(preset.args, "--harness")
        prompt_arg, leftover_args = _extract_arg_flag(leftover_args, "--prompt")
        env = dict(preset.env)
        if harness_arg:
            env[HARNESS_ENV] = harness_arg.strip().lower()
        harness_name = (env.get(HARNESS_ENV) or "").strip().lower()
        if harness_name and harness_name not in known_harnesses():
            known = ", ".join(sorted(known_harnesses()))
            return None, warnings, (
                f"agent '{name}': unknown harness '{harness_name}' "
                f"(known harnesses: {known})"
            )
        ignored = [f for f in ("checkout", "service") if getattr(preset, f)]
        if ignored:
            warnings.append(
                f"agent '{name}': preset field(s) {', '.join(ignored)} ignored — "
                "they configure a session launch, not a spawned child"
            )
        if leftover_args:
            warnings.append(
                f"agent '{name}': preset args {shlex.join(leftover_args)} ignored — "
                "only --harness and --prompt fold into spawned children"
            )
        entry: dict = {"env": env}
        if prompt_arg:
            entry["prompt"] = prompt_arg
        resolved[name] = entry
    return resolved, warnings, None


def _build_preset(name: str, spec: object) -> Preset | None:
    """Validate one raw preset entry into a :class:`Preset`, or ``None``.

    Each problem is logged with the offending preset name so a misconfigured
    entry is easy to find; the entry is skipped rather than aborting the load.
    """
    # A name the selector token can't express would load but never be
    # selectable (and would mislead the "available presets" list), so skip it.
    if not _VALID_NAME_RE.fullmatch(name):
        logger.error("preset_invalid name=%s reason=name_not_selectable", name)
        return None

    if not isinstance(spec, dict):
        logger.error("preset_invalid name=%s reason=not_an_object", name)
        return None

    checkout = spec.get("checkout")
    if checkout is not None and not isinstance(checkout, str):
        logger.error("preset_invalid name=%s reason=checkout_not_a_string", name)
        return None

    service = spec.get("service")
    if service is not None and not isinstance(service, str):
        logger.error("preset_invalid name=%s reason=service_not_a_string", name)
        return None

    # Mirror the lmer CLI rule (--service requires --checkout): a preset that
    # would produce that invalid invocation is rejected up front.
    if service and not checkout:
        logger.error("preset_invalid name=%s reason=service_requires_checkout", name)
        return None

    raw_env = spec.get("env", {})
    if not isinstance(raw_env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()
    ):
        logger.error("preset_invalid name=%s reason=env_not_a_string_map", name)
        return None

    raw_args = spec.get("args", [])
    if not isinstance(raw_args, list) or not all(
        isinstance(a, str) for a in raw_args
    ):
        logger.error("preset_invalid name=%s reason=args_not_a_string_list", name)
        return None

    unknown = set(spec) - _KNOWN_KEYS
    if unknown:
        # Forward-compat / typo signal: keep the preset, but surface the keys.
        logger.warning(
            "preset_unknown_keys name=%s keys=%s", name, ",".join(sorted(unknown))
        )

    return Preset(
        name=name,
        checkout=checkout,
        service=service,
        env=dict(raw_env),
        args=list(raw_args),
    )
