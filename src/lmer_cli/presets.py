"""Named startup presets for spawned ``lmer`` sessions.

A *preset* is an operator-defined, named startup configuration - a local
checkout to mount, a running service container to target (service mode), extra
environment variables, and extra ``lmer`` CLI flags - that a spawner can layer
onto an ``lmer`` invocation by name. Presets live in core lmer so they are not
tied to any single spawner; the host-side Slack listener
(:mod:`slack_chat.listener`), which spawns a repo-less ``lmer chat`` session
per mention/DM, is currently the only consumer.

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

A preset is selected by name via a ``$preset:<name>`` token embedded in a
message, e.g. ``Hey @lmer $preset:my_service please do X`` (the Slack listener
scans the triggering message for it). Preset names must use the selector
charset (``[A-Za-z0-9_-]``); a name with other characters is logged and
skipped at load time, since the token could never select it.

All fields are optional, with one rule mirrored from the lmer CLI: a preset
that sets ``service`` must also set ``checkout`` (``--service`` requires
``--checkout``). Loading degrades gracefully - a missing/unreadable/malformed
file yields no presets, and a single invalid entry is logged and skipped so it
cannot disable the others. A caller that then selects a preset that did not
load gets the consumer's normal "unknown preset" rejection.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("lmer_cli.presets")

# Env var naming the JSON presets file. Read host-side by the listener only;
# it never needs to reach inside a container (the preset's effects do, as the
# --checkout/--service flags and env vars the spawned lmer already forwards).
PRESETS_FILE_ENV = "LMER_PRESETS_FILE"

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
    """A named startup configuration for an lmer chat session.

    Attributes:
        name: The preset's key in the presets file (what ``$preset:<name>``
            selects).
        checkout: Host path to a local source checkout, passed as
            ``--checkout``. Required whenever ``service`` is set.
        service: Docker/Compose service or container name to target, passed
            as ``--service`` (service mode).
        env: Extra environment variables merged into the spawned process's
            environment, overriding inherited values. Use for "other startup
            variables" such as ``LMER_LLM_NAME`` or ``LMER_REASONING_EFFORT``.
        args: Extra CLI tokens appended verbatim to the ``lmer chat`` command
            (e.g. ``["--ports", "2"]``).
    """

    name: str
    checkout: str | None = None
    service: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)


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
