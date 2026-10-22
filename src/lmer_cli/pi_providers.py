"""Provider configuration and model discovery for the pi harness (issue #490).

pi reaches self-hosted OpenAI-compatible providers through a hand-written
``~/.pi/agent/models.json``. That file hard-codes every model id, so a provider
that replaces its models leaves the registry stale and a spawned session dies
on arrival with an unknown-model error — and, when the file carries a masked
``apiKey`` placeholder, with a 401 the operator has to go looking for.

This module moves the provider *definition* into lmer configuration and the
*model list* out of configuration entirely:

- The host keeps a provider registry (name, base URL, API flavour, and the
  environment variable its key lives in) in a JSON file named by
  :data:`PROVIDERS_FILE_ENV`. The key value itself is read from the ordinary
  environment (or a ``.env`` file) and forwarded under a fixed, lmer-owned
  variable name; it never lands in a file the operator or a chat view can see.
- At container start the pi runner calls :func:`main` with ``render``: it
  queries each provider for its current model list, merges any host
  ``models.json`` overlay (host entries win), and writes the container's
  ``models.json``. The generated ``apiKey`` is always a ``$VAR`` reference to
  the lmer-owned key variable.
- The same code validates the result before pi starts — a placeholder
  ``apiKey``, or a ``<provider>/<model>`` name whose configured provider does
  not advertise that model, aborts the launch with the reason and the list of
  what *is* available. Any other model name — including one on a provider pi
  ships itself, whose built-in models pi keeps — is left to pi to resolve.

The provider registry file shape::

    {
      "skynet": {
        "name": "Skynet",
        "baseUrl": "https://skynet.example/v1",
        "api": "openai-completions",
        "keyEnv": "SKYNET_KEY",
        "authHeader": true
      },
      "llamacpp": {
        "baseUrl": "http://192.168.1.50:8080/v1",
        "api": "openai-completions",
        "apiKey": "local"
      }
    }

A provider with ``keyEnv`` is authenticated: its key is read from that variable
and forwarded to the container as ``LMER_PROVIDER_KEY_<NAME>``. A provider with
a literal ``apiKey`` (an unauthenticated local server) keeps that value. An
explicit ``models`` list may be given for a provider that cannot be queried; it
is used as the fallback when discovery returns nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from .util import env_name_segment

logger = logging.getLogger("lmer_cli.pi_providers")

#: Host-side path to the provider registry JSON. When unset, the default
#: ``~/.lmer/pi-providers.json`` is consulted; when that does not exist the
#: feature is off and pi keeps its built-in catalog.
PROVIDERS_FILE_ENV = "LMER_PI_PROVIDERS_FILE"

#: Default provider registry location under the lmer home.
DEFAULT_PROVIDERS_FILE = "~/.lmer/pi-providers.json"

#: Container-side variable carrying the provider metadata (never a secret) as a
#: single-line JSON object, written by the host CLI and read by :func:`main`.
PROVIDERS_ENV = "LMER_PI_PROVIDERS"

#: Prefix for the lmer-owned container variable that holds one provider's key.
#: The generated ``models.json`` references it as ``$<NAME>`` so the value never
#: appears in the file.
KEY_ENV_PREFIX = "LMER_PROVIDER_KEY_"

#: Container path of the host ``models.json`` overlay. The host file is mounted
#: here rather than at ``~/.pi/agent/models.json`` so the runner can write the
#: merged registry to pi's real path (issue #490).
HOST_OVERLAY_PATH = "~/.pi/agent/models.host.json"

#: Container path pi reads its registry from.
CONTAINER_MODELS_PATH = "~/.pi/agent/models.json"

#: Host path of the hand-written overlay, validated host-side before launch.
HOST_MODELS_PATH = "~/.pi/agent/models.json"

#: Default request timeout for a provider probe, in seconds.
DEFAULT_TIMEOUT = 10.0

#: ``apiKey`` values that are obviously not real credentials. ``<redacted>``
#: (the field-note placeholder) and the empty string are always refused. The
#: other masked-looking words are refused anywhere; ``dummy`` is refused only
#: for a provider lmer knows is authenticated, since it is the documented
#: literal for an unauthenticated local server.
_PLACEHOLDER_RE = re.compile(r"^<.*>$")
_PLACEHOLDER_WORDS = frozenset(
    {"placeholder", "redacted", "changeme", "change-me", "xxx", "todo"}
)
_DUMMY_WORDS = frozenset({"dummy"})

#: A ``$VAR`` or ``${VAR}`` apiKey reference — the two forms pi interpolates
#: (pi ``docs/models.md``).
_KEY_REF_RE = re.compile(
    r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$"
)


class ProviderConfigError(Exception):
    """A provider registry that cannot be used, or a launch that must abort."""


class ModelNotOfferedError(ProviderConfigError):
    """The requested model is not among the registry's models."""


@dataclass(frozen=True)
class Provider:
    """One configured provider."""

    name: str
    base_url: str
    api: str = "openai-completions"
    key_env: Optional[str] = None
    api_key: Optional[str] = None
    auth_header: bool = True
    display_name: Optional[str] = None
    #: Explicit fallback models (used when discovery returns nothing).
    models: tuple = field(default_factory=tuple)

    def key_var(self) -> str:
        """The lmer-owned container variable holding this provider's key."""
        return provider_key_var(self.name)


def provider_key_var(name: str) -> str:
    """Return the container variable name for *name*'s key.

    The segment normalization is shared with the scoped resource-limit and
    preset-selector names (:func:`lmer_cli.util.env_name_segment`), so a
    provider name that is not an identifier still yields a stable variable.
    """
    segment = env_name_segment(name)
    if not segment:
        raise ProviderConfigError(
            f"provider name {name!r} has no usable characters for a variable name"
        )
    return f"{KEY_ENV_PREFIX}{segment}"


def _providers_path(path: Optional[str] = None) -> Optional[Path]:
    raw = path if path is not None else os.environ.get(PROVIDERS_FILE_ENV, "")
    if not raw or not raw.strip():
        raw = DEFAULT_PROVIDERS_FILE
    return Path(raw).expanduser()


def load_providers(
    path: Optional[str] = None,
) -> dict[str, Provider]:
    """Load the provider registry, degrading to ``{}`` on absence or error.

    A missing file is the feature being off, not a failure: pi keeps its
    built-in catalog. A malformed file or entry is logged and skipped so one
    bad provider cannot disable the others.
    """
    file_path = _providers_path(path)
    if file_path is None or not file_path.is_file():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("pi_providers_file_unreadable path=%s error=%s", file_path, exc)
        return {}
    if not isinstance(data, dict):
        logger.error("pi_providers_file_not_object path=%s", file_path)
        return {}

    providers: dict[str, Provider] = {}
    for name, spec in data.items():
        provider = _build_provider(str(name), spec)
        if provider is not None:
            providers[provider.name] = provider
    logger.info(
        "pi_providers_loaded path=%s count=%s names=%s",
        file_path,
        len(providers),
        ",".join(sorted(providers)) or "(none)",
    )
    return providers


def _build_provider(name: str, spec: object) -> Optional[Provider]:
    if not isinstance(spec, dict):
        logger.error("pi_provider_invalid name=%s reason=not_an_object", name)
        return None
    base_url = spec.get("baseUrl")
    if not isinstance(base_url, str) or not base_url.strip():
        logger.error("pi_provider_invalid name=%s reason=baseUrl_missing", name)
        return None
    api = spec.get("api", "openai-completions")
    if not isinstance(api, str) or not api.strip():
        logger.error("pi_provider_invalid name=%s reason=api_invalid", name)
        return None
    key_env = spec.get("keyEnv")
    if key_env is not None and (not isinstance(key_env, str) or not key_env.strip()):
        logger.error("pi_provider_invalid name=%s reason=keyEnv_invalid", name)
        return None
    api_key = spec.get("apiKey")
    if api_key is not None and not isinstance(api_key, str):
        logger.error("pi_provider_invalid name=%s reason=apiKey_invalid", name)
        return None
    if key_env and api_key:
        logger.error("pi_provider_invalid name=%s reason=keyEnv_and_apiKey", name)
        return None
    display_name = spec.get("name")
    if display_name is not None and not isinstance(display_name, str):
        logger.error("pi_provider_invalid name=%s reason=name_invalid", name)
        return None
    raw_models = spec.get("models", [])
    if not isinstance(raw_models, list):
        logger.error("pi_provider_invalid name=%s reason=models_not_a_list", name)
        return None
    models = tuple(m for m in raw_models if isinstance(m, dict) and m.get("id"))
    return Provider(
        name=name,
        base_url=base_url.rstrip("/"),
        api=api.strip(),
        key_env=key_env.strip() if key_env else None,
        api_key=api_key,
        auth_header=bool(spec.get("authHeader", True)),
        display_name=display_name,
        models=models,
    )


def _refuse_key_variable_collisions(providers: Mapping[str, Provider]) -> None:
    """Refuse two providers that normalize to the same key variable.

    :func:`provider_key_var` collapses non-alphanumerics, so ``cloud-a`` and
    ``cloud_a`` both yield ``LMER_PROVIDER_KEY_CLOUD_A``. Forwarding would
    overwrite one provider's key with the other's while both metadata entries
    name the same variable, sending a credential across provider boundaries.
    The providers are known here, before any key is
    read or any endpoint is probed, so the collision is a config error.
    """
    owners: dict[str, list[str]] = {}
    for name, provider in providers.items():
        if provider.key_env:
            owners.setdefault(provider.key_var(), []).append(name)
    for key_var, names in owners.items():
        if len(names) > 1:
            listed = ", ".join(repr(name) for name in names)
            raise ProviderConfigError(
                f"provider names {listed} all map to the same key variable "
                f"{key_var}; rename them so one provider's credential cannot "
                "be sent to another's endpoint"
            )


def container_provider_env(
    providers: Mapping[str, Provider],
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Build the container environment carrying providers and their keys.

    Returns the metadata under :data:`PROVIDERS_ENV` (a single-line JSON
    object; never a secret) plus one :data:`KEY_ENV_PREFIX` variable per
    authenticated provider whose key is set. A provider whose ``keyEnv`` names
    an unset or empty variable is forwarded marked ``keyMissing`` and without a
    key: the host cannot tell which provider the session will use, so the
    runner decides — it drops the provider, and aborts naming the variable
    only when the requested model needs it. One unset key must not stop
    sessions that never touch that provider.

    Provider names are normalized to a single variable segment
    (:func:`provider_key_var`), which is many-to-one. Two providers whose names
    normalize together (``cloud-a`` and ``cloud_a``) would share one variable
    and one provider's key would be sent to the other's endpoint, so that
    collision is refused before any value is forwarded.
    """
    env = os.environ if environ is None else environ
    if not providers:
        return {}
    _refuse_key_variable_collisions(providers)
    metadata: dict[str, dict] = {}
    secrets: dict[str, str] = {}
    for name, provider in providers.items():
        entry: dict = {
            "baseUrl": provider.base_url,
            "api": provider.api,
            "authHeader": provider.auth_header,
        }
        if provider.display_name:
            entry["name"] = provider.display_name
        if provider.models:
            entry["models"] = list(provider.models)
        if provider.key_env:
            entry["keyEnv"] = provider.key_env
            value = (env.get(provider.key_env) or "").strip()
            if value:
                key_var = provider.key_var()
                entry["keyVar"] = key_var
                secrets[key_var] = value
            else:
                logger.warning(
                    "pi_provider_key_missing name=%s key_env=%s", name, provider.key_env
                )
                entry["keyMissing"] = True
        elif provider.api_key:
            entry["apiKey"] = provider.api_key
        metadata[name] = entry
    result = {PROVIDERS_ENV: json.dumps(metadata, separators=(",", ":"))}
    result.update(secrets)
    return result


# --- discovery ---------------------------------------------------------------

#: Headers sent with every probe. A browser-like User-Agent avoids the
#: Cloudflare bot block (HTTP 403, code 1010) that the default Python
#: ``urllib``/``requests`` agent triggers in front of some providers.
_PROBE_HEADERS = {"User-Agent": "lmer/pi-provider-discovery"}


def _default_http_get(url: str, headers: Mapping[str, str], timeout: float) -> dict:
    # deferred: import cost — requests adds ~110 ms
    import requests

    try:
        response = requests.get(url, headers=dict(headers), timeout=timeout)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise ProviderUnreachableError(str(exc)) from exc
    if response.status_code in (401, 403):
        raise ProviderAuthError(response.status_code)
    response.raise_for_status()
    return response.json()


class ProviderAuthError(Exception):
    """The provider rejected the request (401/403)."""

    def __init__(self, status: int):
        super().__init__(f"provider returned HTTP {status}")
        self.status = status


class ProviderUnreachableError(Exception):
    """The provider's host could not be connected to, or did not answer in time."""


#: Transport failures that condemn the host rather than one URL on it. Every
#: candidate URL shares the provider's scheme, host and port, so after one of
#: these the others are not tried — an unroutable address costs one timeout,
#: not one per candidate.
_UNREACHABLE = (ProviderUnreachableError, ConnectionError, TimeoutError)


def _candidate_urls(base_url: str, path: str = "models") -> list[str]:
    """Probe ``/<path>`` and the ``/v1/<path>`` variant.

    A provider's configured ``baseUrl`` may or may not carry the ``/v1``
    suffix (pi accepts both), so discovery tries the configured form first and
    the other as a fallback.
    """
    urls = [f"{base_url}/{path}"]
    if base_url.endswith("/v1"):
        urls.append(f"{base_url[:-3]}/{path}")
    else:
        urls.append(f"{base_url}/v1/{path}")
    return urls


def _litellm_context_windows(
    base_url: str,
    headers: Mapping[str, str],
    get: Callable[[str, Mapping[str, str], float], dict],
    timeout: float,
) -> dict[str, int]:
    """Context windows from LiteLLM's ``/model/info``, by model name.

    LiteLLM's ``/models`` carries ids only; the window lives in each
    deployment's ``model_info`` on ``/model/info``. Best effort: a provider
    that is not LiteLLM (404), or a key scoped away from the endpoint
    (401/403), yields ``{}`` and the model list stands — pi then uses its
    default window, as it would without this lookup.
    """
    for url in _candidate_urls(base_url, "model/info"):
        try:
            payload = get(url, headers, timeout)
        except _UNREACHABLE:
            return {}
        except Exception as exc:  # noqa: BLE001 - optional enrichment only
            logger.debug("pi_model_info_unavailable url=%s error=%s", url, exc)
            continue
        windows: dict[str, int] = {}
        for entry in _extract_model_list(payload):
            name = entry.get("model_name")
            if not isinstance(name, str) or not name:
                name = _model_id(entry)
            window = _context_window(entry)
            if name and window and name not in windows:
                windows[name] = window
        if windows:
            return windows
    return {}


def _extract_model_list(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
    return []


def _model_id(entry: Mapping) -> Optional[str]:
    for key in ("id", "model", "model_name", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _context_window(entry: Mapping) -> Optional[int]:
    for key in ("context_window", "contextWindow", "context_length", "max_input_tokens"):
        value = entry.get(key)
        if isinstance(value, int) and value > 0:
            return value
    info = entry.get("model_info")
    if isinstance(info, dict):
        # Not LiteLLM's legacy ``max_tokens``: it is the *output* cap whenever
        # the provider states one, so reading it as the window would shrink an
        # 8K-output model to an 8K window. No input limit means no window, and
        # pi keeps its default.
        for key in ("max_input_tokens", "context_window"):
            value = info.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def discover_models(
    provider: Provider,
    key: Optional[str] = None,
    *,
    http_get: Optional[Callable[[str, Mapping[str, str], float], dict]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    context_windows: bool = True,
) -> list[dict]:
    """Query *provider* for its current models.

    Returns a list of ``{"id": ..., "name": ..., "contextWindow": ...}``
    entries. The configured ``models`` fallback is returned when every probe
    fails with a non-auth error (a provider that cannot be queried). An
    authentication failure (401/403) is raised as :class:`ProviderAuthError`
    so the caller can abort with the key variable named.

    When the model list carries no context windows and *context_windows* is
    true, LiteLLM's ``/model/info`` is asked for them (best effort — see
    :func:`_litellm_context_windows`). A caller that needs ids only (the
    spawn form) passes ``False`` and saves the request.
    """
    get = http_get or _default_http_get
    headers = dict(_PROBE_HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"

    last_error: Optional[Exception] = None
    for url in _candidate_urls(provider.base_url):
        try:
            payload = get(url, headers, timeout)
        except ProviderAuthError:
            raise
        except _UNREACHABLE as exc:
            last_error = exc
            break
        except Exception as exc:  # noqa: BLE001 - any transport error is a fallback
            last_error = exc
            continue
        entries = _extract_model_list(payload)
        if not entries:
            last_error = ProviderConfigError(f"{url} returned no model list")
            continue
        models: list[dict] = []
        for entry in entries:
            model_id = _model_id(entry)
            if not model_id:
                continue
            model: dict = {"id": model_id}
            name = entry.get("name")
            if isinstance(name, str) and name:
                model["name"] = name
            window = _context_window(entry)
            if window:
                model["contextWindow"] = window
            models.append(model)
        if models:
            if context_windows and any("contextWindow" not in m for m in models):
                windows = _litellm_context_windows(
                    provider.base_url, headers, get, timeout
                )
                for model in models:
                    if "contextWindow" not in model and model["id"] in windows:
                        model["contextWindow"] = windows[model["id"]]
            return models
    if provider.models:
        return list(provider.models)
    if last_error is not None:
        raise ProviderConfigError(
            f"could not discover models for provider {provider.name!r}: {last_error}"
        )
    return []


# --- registry rendering and validation ---------------------------------------


def _load_host_overlay(path: Optional[str] = None) -> dict:
    file_path = Path(path if path is not None else HOST_OVERLAY_PATH).expanduser()
    if not file_path.is_file():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderConfigError(
            f"host models.json is not valid JSON ({exc}); refusing to start pi"
        ) from exc
    if not isinstance(data, dict):
        raise ProviderConfigError("host models.json is not a JSON object")
    return data


def validate_host_registry_file(
    path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Validate the host ``~/.pi/agent/models.json`` before the container starts.

    Catches the field-note failure — a literal masked placeholder as an
    ``apiKey`` — without needing the provider network, so a launch that would
    send ``<redacted>`` as a bearer token aborts on the host with the file and
    provider named. Absence is fine; the file is optional config.

    An unset ``$VAR``/``${VAR}`` reference is *not* refused here: that is a provider
    without its key, which the runner drops and treats as fatal only when the
    requested model needs it (see :func:`render`).
    """
    file_path = Path(path if path is not None else HOST_MODELS_PATH).expanduser()
    if not file_path.is_file():
        return
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderConfigError(
            f"{file_path} is not valid JSON ({exc}); refusing to start pi"
        ) from exc
    if not isinstance(data, dict):
        raise ProviderConfigError(f"{file_path} is not a JSON object")
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return
    env = os.environ if environ is None else environ
    for name, block in providers.items():
        if isinstance(block, dict):
            _validate_api_key(
                name, block, env, authenticated=False, check_references=False
            )


def _provider_block(entry: Mapping, models: Iterable[dict]) -> dict:
    block: dict = {
        "baseUrl": entry["baseUrl"],
        "api": entry.get("api", "openai-completions"),
    }
    if entry.get("name"):
        block["name"] = entry["name"]
    if entry.get("authHeader") is not None:
        block["authHeader"] = entry["authHeader"]
    if entry.get("keyVar"):
        block["apiKey"] = f"${entry['keyVar']}"
    elif entry.get("apiKey") is not None:
        block["apiKey"] = entry["apiKey"]
    block["models"] = list(models)
    return block


def _discover_entry(
    name: str,
    entry: Mapping,
    env: Mapping[str, str],
    http_get: Optional[Callable],
    timeout: float,
) -> list[dict]:
    """Discover one metadata entry's models, or raise with the reason."""
    if entry.get("keyMissing"):
        raise ProviderConfigError(
            f"pi provider {name!r}: key variable {entry.get('keyEnv')} is not "
            "set — set it in your environment or .env, or remove keyEnv to use "
            "an unauthenticated provider"
        )
    key_var = entry.get("keyVar")
    key = (env.get(key_var) or "").strip() if key_var else None
    provider = Provider(
        name=name,
        base_url=entry["baseUrl"],
        api=entry.get("api", "openai-completions"),
        models=tuple(entry.get("models") or ()),
    )
    try:
        return discover_models(provider, key, http_get=http_get, timeout=timeout)
    except ProviderAuthError as exc:
        if key_var:
            key_name = entry.get("keyEnv") or key_var
            raise ProviderConfigError(
                f"pi provider {name!r}: key variable {key_name} is set but "
                f"the provider rejected it (HTTP {exc.status})"
            ) from exc
        raise ProviderConfigError(
            f"pi provider {name!r}: provider rejected the request "
            f"(HTTP {exc.status}) and no key is configured"
        ) from exc


def build_registry(
    providers: Mapping[str, Mapping],
    *,
    http_get: Optional[Callable] = None,
    timeout: float = DEFAULT_TIMEOUT,
    host_overlay: Optional[dict] = None,
    environ: Optional[Mapping[str, str]] = None,
    failures: Optional[dict] = None,
) -> dict:
    """Render the effective ``models.json`` for the container.

    *providers* is the metadata shape produced by :func:`container_provider_env`
    (JSON-decoded). Each provider's models are discovered; the host overlay is
    then merged with **host entries winning**, as the operator specified.

    A provider that cannot be used — its key is unset, it rejects the key, or
    it cannot be reached and has no ``models`` fallback — raises
    :class:`ProviderConfigError` by default. When *failures* is given, the
    reason is recorded there under the provider's name instead and the
    provider is left out of the registry, so the caller can decide whether the
    session actually needed it.
    """
    env = os.environ if environ is None else environ
    registry: dict = {"providers": {}}
    for name, entry in providers.items():
        try:
            models = _discover_entry(name, entry, env, http_get, timeout)
        except ProviderConfigError as exc:
            if failures is None:
                raise
            failures[name] = str(exc)
            continue
        registry["providers"][name] = _provider_block(entry, models)

    overlay = host_overlay if host_overlay is not None else {}
    overlay_providers = overlay.get("providers")
    if isinstance(overlay_providers, dict):
        for name, block in overlay_providers.items():
            registry["providers"][name] = block
            if failures is not None:
                # The host entry replaces the discovered one, so the failed
                # discovery no longer describes this provider.
                failures.pop(name, None)
    for key, value in overlay.items():
        if key != "providers":
            registry[key] = value
    return registry


#: Upper bound on the ``pi auth check`` probe behind :func:`pi_defines_provider`.
PI_PROBE_TIMEOUT = 15.0


def pi_defines_provider(
    name: str,
    *,
    run: Optional[Callable] = None,
    timeout: float = PI_PROBE_TIMEOUT,
) -> Optional[bool]:
    """Whether pi itself ships a provider called *name*.

    pi merges a ``models.json`` block for one of its own providers into that
    provider's built-in catalog — even a block that sets ``baseUrl`` and
    ``models`` — so such a block's ``models`` list is not everything the
    provider offers. Asked through ``pi auth check --provider <name> --json``
    against an empty agent directory, so neither the rendered registry nor the
    operator's extensions can make a custom provider look built-in: pi answers
    ``provider_not_found`` for a name it does not ship.

    The cost of the empty directory: a provider an installed extension adds
    reads as not pi's, so a ``baseUrl`` block for it is taken as its whole list.

    Returns ``None`` when pi cannot be asked (not installed, timed out, or an
    answer this code does not recognise); the caller leaves the name to pi.
    """
    runner = run or subprocess.run
    with tempfile.TemporaryDirectory(prefix="lmer-pi-probe-") as agent_dir:
        try:
            result = runner(
                ["pi", "auth", "check", "--provider", name, "--json", "--no-refresh"],
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PI_CODING_AGENT_DIR": agent_dir},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("pi_provider_probe_failed name=%s error=%s", name, exc)
            return None
    for line in (result.stdout or "").splitlines():
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("provider") == name:
            return answer.get("reason") != "provider_not_found"
    logger.warning(
        "pi_provider_probe_unrecognised name=%s rc=%s", name, result.returncode
    )
    return None


def _lists_whole_provider(
    name: str, block: Mapping, pi_provider: Callable[[str], Optional[bool]]
) -> bool:
    """Whether *block*'s ``models`` is everything provider *name* offers.

    Only a block that defines a provider pi does not ship. pi refuses a new
    provider without a ``baseUrl``, so a block without one can only adjust a
    built-in or extension provider (``modelOverrides``, an added model) — and
    a block with one is still merged into pi's catalog when the name is pi's.
    """
    if not block.get("baseUrl"):
        return False
    return pi_provider(name) is False


def validate_registry(
    registry: Mapping,
    requested_model: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
    authenticated: Iterable[str] = (),
    *,
    pi_provider: Optional[Callable[[str], Optional[bool]]] = None,
) -> list[str]:
    """Validate a rendered registry and return the available model ids.

    Raises :class:`ProviderConfigError` when an ``apiKey`` is an obvious
    placeholder or an unset ``$VAR``/``${VAR}`` reference. Returns the available ids
    (bare and ``provider/id`` forms) for the caller to report.

    *requested_model* is checked only when it has the form
    ``<provider>/<model>`` and ``<provider>`` is in this registry; then
    ``<model>`` must be an id that provider offers, optionally with pi's
    ``:<thinking>`` suffix, or :class:`ModelNotOfferedError` is raised. Any
    other name — a pi built-in (``sonnet``), a fuzzy name, a bare id — passes
    through untouched: pi owns model-name resolution, and a copy of its
    resolver here would refuse names pi accepts (decided
    by the owner).

    The same holds for a registry block that only adjusts one of pi's own
    providers (``anthropic`` with ``modelOverrides``): pi keeps that
    provider's built-in models, so the block's list proves nothing and the
    name is left to pi. *pi_provider* answers "does pi ship this provider?"
    (default :func:`pi_defines_provider`); it is asked only for a name that
    would otherwise abort.
    """
    if pi_provider is None:
        pi_provider = pi_defines_provider
    env = os.environ if environ is None else environ
    authed = set(authenticated)
    providers = registry.get("providers") if isinstance(registry, Mapping) else None
    available: list[str] = []
    if not isinstance(providers, dict):
        providers = {}
    for name, block in providers.items():
        if not isinstance(block, dict):
            continue
        _validate_api_key(name, block, env, name in authed)
        models = block.get("models")
        if isinstance(models, list):
            for model in models:
                model_id = _model_id(model) if isinstance(model, dict) else model
                if isinstance(model_id, str) and model_id:
                    available.append(model_id)
                    available.append(f"{name}/{model_id}")

    if requested_model and "/" in requested_model:
        provider_name, model = requested_model.split("/", 1)
        block = providers.get(provider_name)
        if isinstance(block, dict):
            offered = _offered_ids(block)
            if not _names_offered_model(model, offered) and _lists_whole_provider(
                provider_name, block, pi_provider
            ):
                listing = ", ".join(
                    f"{provider_name}/{model_id}" for model_id in sorted(offered)
                ) or "(none discovered)"
                raise ModelNotOfferedError(
                    f"model {model!r} is not offered by pi provider "
                    f"{provider_name!r}. Available: {listing}"
                )
    return available


#: pi's ``--thinking`` levels, accepted as a ``:<level>`` model-name suffix.
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh"})


def _offered_ids(block: Mapping) -> set[str]:
    ids: set[str] = set()
    models = block.get("models")
    if isinstance(models, list):
        for model in models:
            model_id = _model_id(model) if isinstance(model, dict) else model
            if isinstance(model_id, str) and model_id:
                ids.add(model_id)
    return ids


def _names_offered_model(model: str, offered: set[str]) -> bool:
    """Whether *model* (the part after ``provider/``) is one of *offered*.

    An exact id wins first, so an id that itself contains a colon
    (``qwen3:8b``) matches as written; otherwise a trailing ``:<level>`` is
    read as pi's thinking suffix and the id before it must be offered.
    """
    if model in offered:
        return True
    base, sep, level = model.rpartition(":")
    return bool(sep) and level in _THINKING_LEVELS and base in offered


def _key_reference(api_key: str) -> Optional[str]:
    """The environment variable *api_key* references, or ``None``.

    pi resolves both ``$NAME`` and ``${NAME}`` (pi ``docs/models.md``); the
    braced form is a reference, not a literal, so it gets the same preflight.
    """
    match = _KEY_REF_RE.match(api_key.strip())
    if not match:
        return None
    return match.group(1) or match.group(2)


def _unset_key_reference(
    name: str, block: Mapping, env: Mapping[str, str]
) -> Optional[str]:
    """The reason *block*'s ``$VAR``/``${VAR}`` apiKey cannot resolve, or ``None``."""
    api_key = block.get("apiKey")
    if not isinstance(api_key, str):
        return None
    referenced = _key_reference(api_key)
    if referenced and not (env.get(referenced) or "").strip():
        return (
            f"models.json: provider {name} apiKey references "
            f"${referenced} but that variable is not set in the container"
        )
    return None


def _validate_api_key(
    name: str,
    block: Mapping,
    env: Mapping[str, str],
    authenticated: bool = False,
    check_references: bool = True,
) -> None:
    api_key = block.get("apiKey")
    if api_key is None:
        return
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderConfigError(
            f"models.json: provider {name} apiKey is empty, not a $VAR reference"
        )
    if _key_reference(api_key) is not None:
        unset = _unset_key_reference(name, block, env) if check_references else None
        if unset:
            raise ProviderConfigError(unset)
        return
    if _PLACEHOLDER_RE.match(api_key.strip()):
        raise ProviderConfigError(
            f"models.json: provider {name} apiKey is a placeholder "
            f"({api_key.strip()!r}), not a $VAR reference"
        )
    lowered = api_key.strip().lower()
    if lowered in _PLACEHOLDER_WORDS or (authenticated and lowered in _DUMMY_WORDS):
        raise ProviderConfigError(
            f"models.json: provider {name} apiKey is a placeholder "
            f"({api_key.strip()!r}), not a $VAR reference"
        )


# --- container-side entry point ---------------------------------------------


def _read_providers_env(environ: Mapping[str, str]) -> dict:
    raw = (environ.get(PROVIDERS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ProviderConfigError(
            f"{PROVIDERS_ENV} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ProviderConfigError(f"{PROVIDERS_ENV} is not a JSON object")
    return data


def _atomic_write(target: Path, text: str) -> None:
    """Publish *text* at *target* so readers only ever see a complete file.

    Every pi child of a non-pi parent renders the same shared registry, and
    pi reads it at its own start. A truncating ``write_text`` lets a reader
    scheduled inside the write see an empty or partial file, and pi treats
    unparseable JSON as "no custom providers" rather than failing loudly.
    Write a sibling temp file in the same directory and rename it over the
    target — atomic within a filesystem, and visible under the target's
    name to readers that resolved the path already. An existing file's mode
    is carried onto the temp file so the replacement keeps its permissions.
    """
    mode = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def render(
    *,
    environ: Optional[Mapping[str, str]] = None,
    http_get: Optional[Callable] = None,
    overlay_path: Optional[str] = None,
    output_path: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    report: Optional[Callable[[str], None]] = None,
    pi_provider: Optional[Callable[[str], Optional[bool]]] = None,
) -> list[str]:
    """Render and validate the container registry. Returns available models.

    A no-op when no providers are configured and no host overlay exists: pi's
    built-in catalog is left alone.

    A provider that cannot be used right now — unreachable, key unset, key
    rejected, or a ``$VAR``/``${VAR}`` apiKey whose variable is unset — is left out of the
    registry and reported through *report* (stderr by default). It aborts the
    launch only when ``LMER_LLM_NAME`` names it as ``<provider>/…``; a bare
    name is pi's to resolve (see :func:`validate_registry`), and the warning
    is what explains a pi "model not found" if the name was that provider's.
    An obvious placeholder apiKey is a config error and always aborts.

    *pi_provider* is passed to :func:`validate_registry`.
    """
    env = os.environ if environ is None else environ
    emit = report or (lambda line: sys.stderr.write(line + "\n"))
    providers = _read_providers_env(env)
    overlay = _load_host_overlay(overlay_path)
    if not providers and not overlay:
        return []

    failures: dict[str, str] = {}
    registry = build_registry(
        providers, http_get=http_get, timeout=timeout,
        host_overlay=overlay, environ=env, failures=failures,
    )
    for name, block in list(registry["providers"].items()):
        unset = _unset_key_reference(name, block, env) if isinstance(block, dict) else None
        if unset:
            failures[name] = unset
            del registry["providers"][name]

    requested = (env.get("LMER_LLM_NAME") or "").strip() or None
    if requested and "/" in requested:
        owner = requested.split("/", 1)[0]
        if owner in failures:
            raise ProviderConfigError(failures[owner])
    authenticated = {name for name, entry in providers.items() if entry.get("keyVar")}
    available = validate_registry(
        registry, requested, env, authenticated, pi_provider=pi_provider
    )
    for name, reason in sorted(failures.items()):
        emit(f"⚠️  pi provider {name!r} skipped for this session: {reason}")

    target = Path(output_path if output_path is not None else CONTAINER_MODELS_PATH)
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, json.dumps(registry, indent=2) + "\n")
    return available


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m lmer_cli.pi_providers render`` — container-side entry point."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "render":
        sys.stderr.write("usage: python -m lmer_cli.pi_providers render\n")
        return 2
    try:
        available = render()
    except ProviderConfigError as exc:
        sys.stderr.write(f"❌ {exc}\n")
        return 1
    if available:
        sys.stdout.write(f"✅ pi models discovered: {', '.join(sorted(set(available)))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
