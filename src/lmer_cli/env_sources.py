"""Keep file-default provenance across the platform's launcher boundary."""

import hashlib
import json
import os
from collections.abc import Mapping, MutableMapping


DEFAULT_SOURCES_ENV = "LMER_PLATFORM_ENV_DEFAULTS"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sources(env: Mapping[str, str]) -> dict[str, str]:
    try:
        payload = json.loads(env.get(DEFAULT_SOURCES_ENV, "{}"))
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: entry[0] for key, entry in payload.items()
        if key != DEFAULT_SOURCES_ENV and key in env
        and isinstance(entry, list) and len(entry) == 2
        and all(isinstance(value, str) for value in entry)
        and entry[1] == _digest(env[key])
    }


def _write_sources(env: MutableMapping[str, str], sources: dict[str, str]) -> None:
    if sources:
        # Fingerprints detect later replacements without duplicating secret values.
        env[DEFAULT_SOURCES_ENV] = json.dumps({
            key: [source, _digest(env[key])] for key, source in sources.items()
            if key != DEFAULT_SOURCES_ENV and key in env
        })
    else:
        env.pop(DEFAULT_SOURCES_ENV, None)


def remember_env_defaults(sources: dict[str, str]) -> None:
    """Record only values the daemon's default loader actually supplied."""
    _write_sources(os.environ, {**_sources(os.environ), **sources})


def env_with_overrides(overrides: dict[str, str]) -> dict[str, str]:
    """Inherit defaults while protecting every invocation-owned key."""
    defaults = _sources(os.environ)
    env = {**os.environ, **overrides}
    # Ownership matters even when an invocation sets the same value as a default.
    _write_sources(env, {key: source for key, source in defaults.items() if key not in overrides})
    return env


def consume_env_defaults() -> dict[str, str]:
    """Recover source labels once, without forwarding the carrier into containers."""
    sources = _sources(os.environ)
    os.environ.pop(DEFAULT_SOURCES_ENV, None)
    return sources
