"""Tests for pi provider configuration and launch-time model discovery (#490).

The failure signatures these encode:

- a requested model the provider no longer offers (``Model "…" not found``)
- a masked ``apiKey`` placeholder such as ``<redacted>`` sent as a bearer
  token (``401 … expected to start with 'sk-'``)

Both used to produce a live session that held a slot and could not act.
"""

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli import pi_providers as pp

#: What the fake below says pi ships. The real answer comes from
#: ``pi auth check`` (see TestPiDefinesProvider); the rest of this module must
#: not depend on whether pi is installed where the suite runs.
_PI_BUILT_IN_PROVIDERS = frozenset({"anthropic", "openai", "google", "ollama"})

_REAL_PI_DEFINES_PROVIDER = pp.pi_defines_provider


@pytest.fixture(autouse=True)
def _fake_pi_provider_probe(monkeypatch):
    calls: list = []

    def fake(name, **kwargs):
        calls.append(name)
        return name in _PI_BUILT_IN_PROVIDERS

    monkeypatch.setattr(pp, "pi_defines_provider", fake)
    return calls


def _write_providers(tmp_path, data):
    path = tmp_path / "pi-providers.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestProviderKeyVar:
    def test_segment_is_normalized(self):
        assert pp.provider_key_var("skynet") == "LMER_PROVIDER_KEY_SKYNET"
        assert pp.provider_key_var("my-provider") == "LMER_PROVIDER_KEY_MY_PROVIDER"

    def test_unnamable_provider_is_refused(self):
        with pytest.raises(pp.ProviderConfigError):
            pp.provider_key_var("---")


class TestLoadProviders:
    def test_loads_a_provider(self, tmp_path):
        path = _write_providers(tmp_path, {
            "skynet": {
                "baseUrl": "https://skynet.example/v1",
                "api": "openai-completions",
                "keyEnv": "SKYNET_KEY",
            }
        })
        providers = pp.load_providers(str(path))
        assert set(providers) == {"skynet"}
        provider = providers["skynet"]
        assert provider.base_url == "https://skynet.example/v1"
        assert provider.key_env == "SKYNET_KEY"

    def test_missing_file_is_feature_off(self, tmp_path):
        assert pp.load_providers(str(tmp_path / "nope.json")) == {}

    def test_malformed_file_degrades(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert pp.load_providers(str(path)) == {}

    def test_invalid_entry_skipped_not_fatal(self, tmp_path):
        path = _write_providers(tmp_path, {
            "good": {"baseUrl": "https://good.example"},
            "bad": {"keyEnv": "X"},
        })
        assert set(pp.load_providers(str(path))) == {"good"}

    def test_keyEnv_and_apiKey_are_exclusive(self, tmp_path):
        path = _write_providers(tmp_path, {
            "both": {"baseUrl": "https://x", "keyEnv": "K", "apiKey": "literal"},
        })
        assert pp.load_providers(str(path)) == {}


class TestContainerProviderEnv:
    def test_metadata_and_secret_are_separate(self):
        providers = {
            "skynet": pp.Provider(
                name="skynet",
                base_url="https://skynet.example/v1",
                key_env="SKYNET_KEY",
            )
        }
        env = pp.container_provider_env(providers, {"SKYNET_KEY": "sk-secret"})
        metadata = json.loads(env[pp.PROVIDERS_ENV])
        assert metadata["skynet"]["keyVar"] == "LMER_PROVIDER_KEY_SKYNET"
        assert metadata["skynet"]["keyEnv"] == "SKYNET_KEY"
        assert "sk-secret" not in env[pp.PROVIDERS_ENV]
        assert env["LMER_PROVIDER_KEY_SKYNET"] == "sk-secret"

    def test_missing_key_is_forwarded_as_unusable_not_fatal(self):
        """A provider whose key is unset must not abort launches that never
        use it: it is forwarded without a key, marked so
        the runner can name the variable if the requested model needs it."""
        providers = {
            "skynet": pp.Provider(
                name="skynet", base_url="https://x", key_env="SKYNET_KEY"
            ),
            "other": pp.Provider(
                name="other", base_url="https://y", key_env="OTHER_KEY"
            ),
        }
        env = pp.container_provider_env(providers, {"SKYNET_KEY": "sk-secret"})
        metadata = json.loads(env[pp.PROVIDERS_ENV])
        assert metadata["skynet"]["keyVar"] == "LMER_PROVIDER_KEY_SKYNET"
        assert metadata["other"]["keyMissing"] is True
        assert metadata["other"]["keyEnv"] == "OTHER_KEY"
        assert "keyVar" not in metadata["other"]
        assert "LMER_PROVIDER_KEY_OTHER" not in env

    def test_unauthenticated_provider_keeps_literal(self):
        providers = {
            "llamacpp": pp.Provider(
                name="llamacpp", base_url="http://127.0.0.1:8080/v1", api_key="local"
            )
        }
        env = pp.container_provider_env(providers, {})
        metadata = json.loads(env[pp.PROVIDERS_ENV])
        assert metadata["llamacpp"]["apiKey"] == "local"
        assert pp.PROVIDERS_ENV in env

    def test_distinct_providers_keep_distinct_keys(self):
        """The noncolliding multi-provider path must stay intact."""
        providers = {
            "cloud-a": pp.Provider(
                name="cloud-a", base_url="https://a/v1", key_env="A_KEY"
            ),
            "cloud_b": pp.Provider(
                name="cloud_b", base_url="https://b/v1", key_env="B_KEY"
            ),
        }
        env = pp.container_provider_env(
            providers, {"A_KEY": "sk-a", "B_KEY": "sk-b"}
        )
        metadata = json.loads(env[pp.PROVIDERS_ENV])
        assert metadata["cloud-a"]["keyVar"] == "LMER_PROVIDER_KEY_CLOUD_A"
        assert metadata["cloud_b"]["keyVar"] == "LMER_PROVIDER_KEY_CLOUD_B"
        assert env["LMER_PROVIDER_KEY_CLOUD_A"] == "sk-a"
        assert env["LMER_PROVIDER_KEY_CLOUD_B"] == "sk-b"

    def test_colliding_normalized_key_variables_are_refused(self):
        """``cloud-a`` and ``cloud_a`` both normalize to ``CLOUD_A``; forwarding
        them would silently overwrite A's key with B's, sending one provider's
        credential to the other's endpoint."""
        providers = {
            "cloud-a": pp.Provider(
                name="cloud-a", base_url="https://a/v1", key_env="A_KEY"
            ),
            "cloud_a": pp.Provider(
                name="cloud_a", base_url="https://b/v1", key_env="B_KEY"
            ),
        }
        with pytest.raises(pp.ProviderConfigError) as excinfo:
            pp.container_provider_env(
                providers, {"A_KEY": "sk-a", "B_KEY": "sk-b"}
            )
        message = str(excinfo.value)
        assert "CLOUD_A" in message
        assert "cloud-a" in message and "cloud_a" in message


class TestDiscoverModels:
    def test_parses_openai_model_list(self):
        def get(url, headers, timeout):
            assert url == "https://p.example/v1/models"
            return {"data": [{"id": "a"}, {"id": "b", "context_window": 1000}]}

        provider = pp.Provider(name="p", base_url="https://p.example/v1")
        models = pp.discover_models(provider, http_get=get)
        assert [m["id"] for m in models] == ["a", "b"]
        assert models[1]["contextWindow"] == 1000

    def test_sends_bearer_header(self):
        seen = {}

        def get(url, headers, timeout):
            seen.update(headers)
            return {"data": [{"id": "a"}]}

        provider = pp.Provider(name="p", base_url="https://p.example/v1")
        pp.discover_models(provider, "sk-key", http_get=get)
        assert seen["Authorization"] == "Bearer sk-key"

    def test_falls_back_to_v1_variant(self):
        calls = []

        def get(url, headers, timeout):
            calls.append(url)
            if url.endswith("/v1/models"):
                return {"data": [{"id": "a"}]}
            raise RuntimeError("404")

        provider = pp.Provider(name="p", base_url="https://p.example")
        models = pp.discover_models(provider, http_get=get)
        assert [m["id"] for m in models] == ["a"]
        assert calls[:2] == ["https://p.example/models", "https://p.example/v1/models"]

    def test_auth_error_propagates(self):
        def get(url, headers, timeout):
            raise pp.ProviderAuthError(401)

        provider = pp.Provider(name="p", base_url="https://p.example/v1")
        with pytest.raises(pp.ProviderAuthError):
            pp.discover_models(provider, http_get=get)

    # --- LiteLLM /model/info context windows ---------------------------------

    @staticmethod
    def _litellm(calls, *, model_info=None):
        def get(url, headers, timeout):
            calls.append(url)
            if url == "https://skynet.example/v1/models":
                # LiteLLM's /models carries ids only — no context window.
                return {"data": [{"id": "deepseek-v4-flash"}, {"id": "qwen38"}]}
            if url.endswith("/model/info"):
                if isinstance(model_info, Exception):
                    raise model_info
                if model_info is not None:
                    return model_info
            raise RuntimeError("404")
        return get

    _MODEL_INFO = {"data": [
        {"model_name": "deepseek-v4-flash",
         "litellm_params": {"model": "openai/deepseek"},
         "model_info": {"id": "a1b2", "max_input_tokens": 1_000_000,
                        "max_tokens": 8192}},
        {"model_name": "qwen38",
         "model_info": {"id": "c3d4", "max_input_tokens": None}},
    ]}

    def test_litellm_model_info_supplies_context_windows(self):
        calls: list = []
        provider = pp.Provider(name="skynet", base_url="https://skynet.example/v1")
        models = pp.discover_models(
            provider, "sk-key",
            http_get=self._litellm(calls, model_info=self._MODEL_INFO),
        )
        by_id = {m["id"]: m for m in models}
        assert by_id["deepseek-v4-flash"]["contextWindow"] == 1_000_000
        # A null window leaves pi's default rather than inventing one.
        assert "contextWindow" not in by_id["qwen38"]
        assert "https://skynet.example/v1/model/info" in calls

    @pytest.mark.parametrize(
        "failure", [RuntimeError("404"), pp.ProviderAuthError(403)]
    )
    def test_model_info_failure_keeps_the_model_list(self, failure):
        """/model/info is LiteLLM-only and may be refused to a scoped key; the
        model list /models returned stands either way."""
        calls: list = []
        provider = pp.Provider(name="skynet", base_url="https://skynet.example/v1")
        models = pp.discover_models(
            provider, "sk-key", http_get=self._litellm(calls, model_info=failure)
        )
        assert [m["id"] for m in models] == ["deepseek-v4-flash", "qwen38"]
        assert all("contextWindow" not in m for m in models)

    @pytest.mark.parametrize("model_info, window", [
        ({"max_input_tokens": 1_000_000, "max_output_tokens": 8192,
          "max_tokens": 8192}, 1_000_000),
        # LiteLLM's legacy max_tokens is the output cap when one is stated:
        # it must not become the window (round-2 review, pi_providers.py:406).
        ({"max_input_tokens": None, "max_output_tokens": 8192,
          "max_tokens": 8192}, None),
        ({"max_tokens": 8192}, None),
        ({"max_input_tokens": None, "max_output_tokens": None,
          "max_tokens": None}, None),
    ])
    def test_model_info_window_is_the_input_limit_only(self, model_info, window):
        payload = {"data": [{"model_name": "deepseek-v4-flash",
                             "model_info": model_info}]}
        provider = pp.Provider(name="skynet", base_url="https://skynet.example/v1")
        models = pp.discover_models(
            provider, "sk-key", http_get=self._litellm([], model_info=payload)
        )
        by_id = {m["id"]: m for m in models}
        assert by_id["deepseek-v4-flash"].get("contextWindow") == window

    def test_model_info_skipped_when_windows_not_wanted(self):
        calls: list = []
        provider = pp.Provider(name="skynet", base_url="https://skynet.example/v1")
        pp.discover_models(
            provider, "sk-key",
            http_get=self._litellm(calls, model_info=self._MODEL_INFO),
            context_windows=False,
        )
        assert not any(url.endswith("/model/info") for url in calls)

    def test_model_info_not_queried_when_models_carry_windows(self):
        calls: list = []

        def get(url, headers, timeout):
            calls.append(url)
            return {"data": [{"id": "a", "context_window": 4096}]}

        provider = pp.Provider(name="p", base_url="https://p.example/v1")
        pp.discover_models(provider, http_get=get)
        assert calls == ["https://p.example/v1/models"]

    def test_render_writes_the_discovered_context_window(self, tmp_path):
        calls: list = []
        out = tmp_path / "models.json"
        pp.render(
            environ={
                pp.PROVIDERS_ENV: json.dumps({
                    "skynet": {
                        "baseUrl": "https://skynet.example/v1",
                        "keyVar": "LMER_PROVIDER_KEY_SKYNET",
                    }
                }),
                "LMER_PROVIDER_KEY_SKYNET": "sk-secret",
            },
            http_get=self._litellm(calls, model_info=self._MODEL_INFO),
            overlay_path=str(tmp_path / "none.json"),
            output_path=str(out),
        )
        models = json.loads(out.read_text(encoding="utf-8"))["providers"]["skynet"][
            "models"
        ]
        assert {"id": "deepseek-v4-flash", "contextWindow": 1_000_000} in models

    # --- an unreachable host costs one timeout, not one per URL ---------------

    @pytest.mark.parametrize(
        "error", [ConnectionError("connection refused"), TimeoutError("timed out")]
    )
    def test_unreachable_host_is_probed_once(self, error):
        """Both candidate URLs share scheme, host and port: when the first
        cannot connect, the second cannot either, so it is not tried."""
        calls: list = []

        def get(url, headers, timeout):
            calls.append(url)
            raise error

        provider = pp.Provider(name="lan", base_url="http://10.255.255.1:8080/v1")
        with pytest.raises(pp.ProviderConfigError, match="could not discover"):
            pp.discover_models(provider, http_get=get)
        assert calls == ["http://10.255.255.1:8080/v1/models"]

    @pytest.mark.parametrize("exc_name", ["ConnectTimeout", "ConnectionError", "ReadTimeout"])
    def test_default_transport_maps_connect_failures(self, monkeypatch, exc_name):
        import requests

        def boom(*args, **kwargs):
            raise getattr(requests.exceptions, exc_name)("down")

        monkeypatch.setattr(requests, "get", boom)
        with pytest.raises(pp.ProviderUnreachableError):
            pp._default_http_get("http://10.255.255.1:8080/v1/models", {}, 1.0)

    def test_explicit_fallback_used_when_unqueryable(self):
        def get(url, headers, timeout):
            raise RuntimeError("connection refused")

        provider = pp.Provider(
            name="p",
            base_url="http://127.0.0.1:8080/v1",
            models=({"id": "local-model"},),
        )
        models = pp.discover_models(provider, http_get=get)
        assert [m["id"] for m in models] == ["local-model"]


class TestBuildRegistry:
    def _metadata(self, key_var="LMER_PROVIDER_KEY_SKYNET"):
        return {
            "skynet": {
                "baseUrl": "https://skynet.example/v1",
                "api": "openai-completions",
                "authHeader": True,
                "keyVar": key_var,
                "keyEnv": "SKYNET_KEY",
            }
        }

    def test_generated_api_key_is_a_reference(self):
        def get(url, headers, timeout):
            return {"data": [{"id": "m1"}]}

        registry = pp.build_registry(
            self._metadata(),
            http_get=get,
            environ={"LMER_PROVIDER_KEY_SKYNET": "sk-secret"},
        )
        block = registry["providers"]["skynet"]
        assert block["apiKey"] == "$LMER_PROVIDER_KEY_SKYNET"
        assert "sk-secret" not in json.dumps(registry)
        assert [m["id"] for m in block["models"]] == ["m1"]

    def test_host_overlay_wins(self):
        def get(url, headers, timeout):
            return {"data": [{"id": "discovered"}]}

        overlay = {
            "providers": {
                "skynet": {
                    "baseUrl": "https://skynet.example/v1",
                    "api": "openai-completions",
                    "apiKey": "$SKYNET_KEY",
                    "models": [{"id": "host-model"}],
                }
            }
        }
        registry = pp.build_registry(
            self._metadata(),
            http_get=get,
            host_overlay=overlay,
            environ={"LMER_PROVIDER_KEY_SKYNET": "sk-secret"},
        )
        assert registry["providers"]["skynet"]["models"] == [{"id": "host-model"}]

    def test_auth_failure_names_the_key_variable(self):
        def get(url, headers, timeout):
            raise pp.ProviderAuthError(401)

        with pytest.raises(pp.ProviderConfigError, match="SKYNET_KEY is set but"):
            pp.build_registry(
                self._metadata(),
                http_get=get,
                environ={"LMER_PROVIDER_KEY_SKYNET": "sk-secret"},
            )


class TestValidateRegistry:
    def _registry(self, api_key="$SKYNET_KEY"):
        return {
            "providers": {
                "skynet": {
                    "baseUrl": "https://skynet.example/v1",
                    "apiKey": api_key,
                    "models": [{"id": "deepseek-v4-flash"}, {"id": "qwen38"}],
                }
            }
        }

    def test_reference_to_set_variable_passes(self):
        available = pp.validate_registry(
            self._registry(), "deepseek-v4-flash", {"SKYNET_KEY": "sk-x"}
        )
        assert "deepseek-v4-flash" in available
        assert "skynet/deepseek-v4-flash" in available

    def test_placeholder_angle_brackets_is_refused(self):
        with pytest.raises(pp.ProviderConfigError, match="placeholder"):
            pp.validate_registry(
                self._registry(api_key="<redacted>"), None, {"SKYNET_KEY": "sk-x"}
            )

    def test_empty_api_key_is_refused(self):
        with pytest.raises(pp.ProviderConfigError):
            pp.validate_registry(self._registry(api_key=""), None, {})

    def test_unset_reference_is_refused(self):
        with pytest.raises(pp.ProviderConfigError, match="not set in the container"):
            pp.validate_registry(self._registry(), None, {})

    def test_braced_reference_to_set_variable_passes(self):
        """pi accepts ``${NAME}`` as well as ``$NAME`` (pi docs/models.md); a
        set braced reference is the same working key."""
        available = pp.validate_registry(
            self._registry(api_key="${SKYNET_KEY}"), None, {"SKYNET_KEY": "sk-x"}
        )
        assert "skynet/deepseek-v4-flash" in available

    def test_braced_unset_reference_is_refused(self):
        """A ``${VAR}`` whose variable is unset must be caught by the same
        missing-key preflight as ``$VAR``."""
        with pytest.raises(pp.ProviderConfigError, match="not set in the container"):
            pp.validate_registry(self._registry(api_key="${SKYNET_KEY}"), None, {})

    def test_dummy_refused_for_authenticated_provider(self):
        with pytest.raises(pp.ProviderConfigError, match="placeholder"):
            pp.validate_registry(
                self._registry(api_key="dummy"), None, {}, authenticated={"skynet"}
            )

    def test_dummy_allowed_for_unauthenticated_provider(self):
        pp.validate_registry(self._registry(api_key="dummy"), None, {})

    def test_unknown_requested_model_lists_available(self):
        with pytest.raises(pp.ModelNotOfferedError) as excinfo:
            pp.validate_registry(
                self._registry(), "skynet/deepseek-v4.1-flash", {"SKYNET_KEY": "sk-x"}
            )
        message = str(excinfo.value)
        assert "deepseek-v4.1-flash" in message
        assert "skynet/deepseek-v4-flash" in message

    # --- the name is checked only against a configured provider ---------------
    # Decided by the owner: pi owns model-name resolution
    # (built-ins, fuzzy names, ``:<thinking>`` suffixes). lmer checks only a
    # ``provider/model`` name whose provider is one it configured.

    @pytest.mark.parametrize("requested", [
        "sonnet",                        # pi built-in alias
        "claude-sonnet-4-6",             # pi built-in id
        "anthropic/claude-sonnet-4-6",   # built-in provider lmer did not configure
        "deepseek",                      # fuzzy
        "deepseek-v4-flash:high",        # thinking suffix on a bare name
        "deepseek-v4.1-flash",           # bare unknown: pi's call, not lmer's
        "skynet/deepseek-v4-flash:high", # thinking suffix under a configured provider
        "skynet/qwen38",
    ])
    def test_names_pi_resolves_pass_through(self, requested):
        pp.validate_registry(self._registry(), requested, {"SKYNET_KEY": "sk-x"})

    @pytest.mark.parametrize("requested", [
        "skynet/deepseek-v4.1-flash",    # the provider replaced its models
        "skynet/deepseek-v4-flash:bogus",  # not a thinking level, not an id
    ])
    def test_unknown_model_on_configured_provider_fails_fast(self, requested):
        with pytest.raises(pp.ModelNotOfferedError, match="skynet"):
            pp.validate_registry(self._registry(), requested, {"SKYNET_KEY": "sk-x"})

    def test_model_id_containing_a_colon_is_matched_exactly(self):
        registry = {"providers": {"lan-ollama": {
            "baseUrl": "http://192.168.1.60:11434/v1", "apiKey": "local",
            "models": [{"id": "qwen3:8b"}]}}}
        pp.validate_registry(registry, "lan-ollama/qwen3:8b", {})
        with pytest.raises(pp.ModelNotOfferedError):
            pp.validate_registry(registry, "lan-ollama/qwen3:14b", {})

    # --- a block for one of pi's own providers is not its whole list ---------
    # pi merges a models.json block for a provider it ships into that
    # provider's built-in catalog (pi docs/models.md: "A ``models`` entry adds
    # or replaces a model ... Use ``modelOverrides`` ... without replacing the
    # provider's model list"), so the block cannot prove a model absent.

    @pytest.mark.parametrize("block", [
        # Round-2 review evidence: modelOverrides only, no baseUrl.
        {"modelOverrides": {"claude-sonnet-4-6": {"contextWindow": 200000}}},
        # One added model on a built-in provider, no baseUrl.
        {"models": [{"id": "my-finetune"}]},
        # baseUrl and models on a name pi ships: pi still keeps its built-ins.
        {"baseUrl": "https://proxy.example/v1", "api": "anthropic-messages",
         "apiKey": "$ANTHROPIC_PROXY_KEY", "models": [{"id": "my-finetune"}]},
    ])
    @pytest.mark.parametrize("requested", [
        "anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-4-6:high",
    ])
    def test_block_for_a_pi_provider_leaves_its_built_ins_to_pi(
        self, block, requested
    ):
        registry = {"providers": {"anthropic": block}}
        pp.validate_registry(registry, requested, {"ANTHROPIC_PROXY_KEY": "k"})

    def test_block_without_base_url_never_asks_pi(self, _fake_pi_provider_probe):
        """pi refuses a new provider without a baseUrl, so such a block can
        only adjust an existing one — nothing to ask."""
        registry = {"providers": {"my-extension": {"models": [{"id": "a"}]}}}
        pp.validate_registry(registry, "my-extension/b", {})
        assert _fake_pi_provider_probe == []

    def test_offered_model_never_asks_pi(self, _fake_pi_provider_probe):
        pp.validate_registry(
            self._registry(), "skynet/deepseek-v4-flash", {"SKYNET_KEY": "sk-x"}
        )
        assert _fake_pi_provider_probe == []

    def test_unknown_model_on_custom_provider_asks_pi_then_aborts(
        self, _fake_pi_provider_probe
    ):
        with pytest.raises(pp.ModelNotOfferedError, match="skynet"):
            pp.validate_registry(
                self._registry(), "skynet/deepseek-v4.1-flash", {"SKYNET_KEY": "sk-x"}
            )
        assert _fake_pi_provider_probe == ["skynet"]

    def test_pi_unavailable_leaves_the_name_to_pi(self):
        """If pi cannot say whether it ships the provider, lmer cannot tell a
        stale model from a built-in one; pi's own error stands, as before."""
        pp.validate_registry(
            self._registry(), "skynet/deepseek-v4.1-flash", {"SKYNET_KEY": "sk-x"},
            pi_provider=lambda name: None,
        )


class _Completed:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class TestPiDefinesProvider:
    """``pi auth check --provider <name> --json`` against an empty agent dir."""

    @pytest.mark.parametrize("answer, expected", [
        ({"status": "not_ready", "provider": "anthropic",
          "reason": "credentials_not_configured"}, True),
        ({"status": "ready", "provider": "anthropic", "authType": "api_key"}, True),
        ({"status": "not_ready", "provider": "anthropic",
          "reason": "provider_not_found"}, False),
    ])
    def test_reads_pi_s_answer(self, answer, expected):
        seen: dict = {}

        def run(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs["env"]
            seen["agent_dir_contents"] = list(
                Path(kwargs["env"]["PI_CODING_AGENT_DIR"]).iterdir()
            )
            return _Completed(json.dumps(answer) + "\n")

        assert _REAL_PI_DEFINES_PROVIDER("anthropic", run=run) is expected
        assert seen["argv"][:3] == ["pi", "auth", "check"]
        assert seen["argv"][3:5] == ["--provider", "anthropic"]
        # An empty agent dir: the rendered registry and the operator's
        # extensions cannot make a custom provider look like one pi ships.
        assert seen["agent_dir_contents"] == []

    @pytest.mark.parametrize("failure", [
        FileNotFoundError("pi"),
        __import__("subprocess").TimeoutExpired(["pi"], 15),
    ])
    def test_pi_that_cannot_be_asked_is_unknown(self, failure):
        def run(argv, **kwargs):
            raise failure

        assert _REAL_PI_DEFINES_PROVIDER("anthropic", run=run) is None

    @pytest.mark.parametrize("stdout", [
        "", "Error: unknown option --no-refresh\n",
        json.dumps({"status": "ready", "provider": "other"}) + "\n",
    ])
    def test_unrecognised_answer_is_unknown(self, stdout):
        def run(argv, **kwargs):
            return _Completed(stdout, returncode=1)

        assert _REAL_PI_DEFINES_PROVIDER("anthropic", run=run) is None

    @pytest.mark.skipif(
        __import__("shutil").which("pi") is None, reason="pi is not installed"
    )
    def test_against_the_installed_pi(self):
        assert _REAL_PI_DEFINES_PROVIDER("anthropic") is True
        assert _REAL_PI_DEFINES_PROVIDER("lmer-test-no-such-provider") is False


class TestValidateHostRegistryFile:
    def test_placeholder_in_host_file_is_refused(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(json.dumps({
            "providers": {
                "skynet": {"apiKey": "<redacted>", "models": [{"id": "m"}]}
            }
        }), encoding="utf-8")
        with pytest.raises(pp.ProviderConfigError, match="placeholder"):
            pp.validate_host_registry_file(str(path), environ={})

    def test_missing_host_file_is_fine(self, tmp_path):
        pp.validate_host_registry_file(str(tmp_path / "none.json"), environ={})

    def test_unset_reference_in_host_file_is_not_fatal(self, tmp_path):
        """An unset ``$VAR`` is a keyless provider, not a broken file: the
        runner drops it and aborts only if the session needs it."""
        path = tmp_path / "models.json"
        path.write_text(json.dumps({
            "providers": {
                "openai": {"apiKey": "$OPENAI_KEY", "models": [{"id": "m"}]}
            }
        }), encoding="utf-8")
        pp.validate_host_registry_file(str(path), environ={})


class TestRender:
    def test_renders_and_validates(self, tmp_path):
        def get(url, headers, timeout):
            return {"data": [{"id": "m1"}]}

        out = tmp_path / "out" / "models.json"
        available = pp.render(
            environ={
                pp.PROVIDERS_ENV: json.dumps({
                    "skynet": {
                        "baseUrl": "https://skynet.example/v1",
                        "keyVar": "LMER_PROVIDER_KEY_SKYNET",
                        "keyEnv": "SKYNET_KEY",
                    }
                }),
                "LMER_PROVIDER_KEY_SKYNET": "sk-secret",
                "LMER_LLM_NAME": "m1",
            },
            http_get=get,
            overlay_path=str(tmp_path / "none.json"),
            output_path=str(out),
        )
        assert "m1" in available
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["providers"]["skynet"]["apiKey"] == "$LMER_PROVIDER_KEY_SKYNET"

    def test_no_config_is_a_noop(self, tmp_path):
        out = tmp_path / "models.json"
        assert pp.render(
            environ={},
            overlay_path=str(tmp_path / "none.json"),
            output_path=str(out),
        ) == []
        assert not out.exists()

    def test_unknown_model_aborts_before_writing(self, tmp_path):
        def get(url, headers, timeout):
            return {"data": [{"id": "m1"}]}

        out = tmp_path / "models.json"
        with pytest.raises(pp.ProviderConfigError, match="not offered"):
            pp.render(
                environ={
                    pp.PROVIDERS_ENV: json.dumps({
                        "skynet": {
                            "baseUrl": "https://skynet.example/v1",
                            "keyVar": "LMER_PROVIDER_KEY_SKYNET",
                        }
                    }),
                    "LMER_PROVIDER_KEY_SKYNET": "sk-secret",
                    "LMER_LLM_NAME": "skynet/deepseek-v4.1-flash",
                },
                http_get=get,
                overlay_path=str(tmp_path / "none.json"),
                output_path=str(out),
            )
        assert not out.exists()

    def test_braced_unset_overlay_reference_skips_an_unused_provider(self, tmp_path):
        """A host overlay ``${VAR}`` with the variable unset is the same
        unusable provider as ``$VAR``: dropped and reported, not written."""
        overlay = {
            "providers": {
                "fixture-local": {
                    "baseUrl": "http://127.0.0.1:8080/v1",
                    "apiKey": "${FIXTURE_UNSET}",
                    "models": [{"id": "fixture-model"}],
                }
            }
        }
        out = tmp_path / "models.json"
        reported: list[str] = []
        available = pp.render(
            environ={},
            overlay_path=self._write_overlay(tmp_path, overlay),
            output_path=str(out),
            report=reported.append,
        )
        written = json.loads(out.read_text(encoding="utf-8"))
        assert "fixture-local" not in written["providers"]
        assert "fixture-local" not in available
        assert any("FIXTURE_UNSET" in line for line in reported)

    @staticmethod
    def _write_overlay(tmp_path, overlay):
        path = tmp_path / "models.host.json"
        path.write_text(json.dumps(overlay), encoding="utf-8")
        return str(path)

    # --- one provider's failure costs only launches that use it ---------------

    _SKYNET = {
        "baseUrl": "https://skynet.example/v1",
        "keyVar": "LMER_PROVIDER_KEY_SKYNET",
        "keyEnv": "SKYNET_KEY",
    }
    _LLAMACPP = {"baseUrl": "http://192.168.1.50:8080/v1", "apiKey": "local"}

    @staticmethod
    def _llamacpp_down(url, headers, timeout):
        if "192.168.1.50" in url:
            raise ConnectionError("connection refused")
        return {"data": [{"id": "deepseek-v4-flash"}]}

    def _render(self, tmp_path, providers, *, requested=None, http_get,
                overlay=None, extra_env=None, report=None):
        environ = {
            pp.PROVIDERS_ENV: json.dumps(providers),
            "LMER_PROVIDER_KEY_SKYNET": "sk-secret",
        }
        if requested:
            environ["LMER_LLM_NAME"] = requested
        environ.update(extra_env or {})
        overlay_path = tmp_path / "overlay.json"
        if overlay is not None:
            overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
        out = tmp_path / "models.json"
        available = pp.render(
            environ=environ,
            http_get=http_get,
            overlay_path=str(overlay_path),
            output_path=str(out),
            report=report,
        )
        return available, out

    @pytest.mark.parametrize("requested", ["deepseek-v4-flash", None])
    def test_unreachable_unrelated_provider_does_not_abort(self, tmp_path, requested):
        reports: list = []
        available, out = self._render(
            tmp_path,
            {"skynet": self._SKYNET, "llamacpp": self._LLAMACPP},
            requested=requested,
            http_get=self._llamacpp_down,
            report=reports.append,
        )
        assert "skynet/deepseek-v4-flash" in available
        written = json.loads(out.read_text(encoding="utf-8"))
        assert set(written["providers"]) == {"skynet"}
        # The skip is visible, not silent.
        assert any("llamacpp" in line and "connection refused" in line
                   for line in reports)

    def test_unreachable_provider_in_use_aborts_with_its_reason(self, tmp_path):
        with pytest.raises(pp.ProviderConfigError, match="connection refused"):
            self._render(
                tmp_path,
                {"skynet": self._SKYNET, "llamacpp": self._LLAMACPP},
                requested="llamacpp/qwen3-coder",
                http_get=self._llamacpp_down,
            )
        assert not (tmp_path / "models.json").exists()

    def test_bare_name_is_pi_s_to_resolve_even_with_a_provider_skipped(
        self, tmp_path
    ):
        """A bare name is never checked: it launches, and the
        skipped provider's warning is what explains a later pi "not found"."""
        reports: list = []
        self._render(
            tmp_path,
            {"skynet": self._SKYNET, "llamacpp": self._LLAMACPP},
            requested="qwen3-coder",
            http_get=self._llamacpp_down,
            report=reports.append,
        )
        assert any("llamacpp" in line for line in reports)

    @pytest.mark.parametrize("requested", ["sonnet", "anthropic/claude-sonnet-4-6",
                                           "deepseek-v4-flash:high", "deepseek"])
    def test_built_in_and_pi_resolved_names_launch(self, tmp_path, requested):
        """Review evidence: these aborted while any provider was
        configured. pi resolves them; lmer must not refuse them."""
        available, out = self._render(
            tmp_path, {"skynet": self._SKYNET},
            requested=requested, http_get=self._llamacpp_down,
        )
        assert out.exists()
        assert "skynet/deepseek-v4-flash" in available

    @pytest.mark.parametrize("requested", ["sonnet", "claude-sonnet-4-6",
                                           "openai/gpt-5.5"])
    def test_host_models_json_only_keeps_built_in_names(self, tmp_path, requested):
        """The "existing setups keep working" promise: a host models.json with
        one llama.cpp provider and no pi-providers.json."""
        overlay_path = tmp_path / "models.host.json"
        overlay_path.write_text(json.dumps({"providers": {"llamacpp": {
            "baseUrl": "http://192.168.1.50:8080/v1", "api": "openai-completions",
            "apiKey": "local", "models": [{"id": "qwen3-coder"}],
        }}}), encoding="utf-8")
        out = tmp_path / "models.json"
        pp.render(
            environ={"LMER_LLM_NAME": requested},
            overlay_path=str(overlay_path),
            output_path=str(out),
        )
        assert out.exists()

    @pytest.mark.parametrize("requested", [
        "anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-5",
        "claude-sonnet-4-6", "sonnet",
    ])
    def test_host_block_adjusting_a_pi_provider_keeps_its_models(
        self, tmp_path, requested
    ):
        """Round-2 review evidence: a host models.json holding only
        ``modelOverrides`` for pi's ``anthropic`` aborted
        ``anthropic/claude-sonnet-4-6`` with "(none discovered)". pi keeps the
        built-ins, and on prep-release this launched."""
        overlay_path = tmp_path / "models.host.json"
        overlay_path.write_text(json.dumps({"providers": {"anthropic": {
            "modelOverrides": {"claude-sonnet-4-6": {"contextWindow": 200000}},
        }}}), encoding="utf-8")
        out = tmp_path / "models.json"
        pp.render(
            environ={"LMER_LLM_NAME": requested},
            overlay_path=str(overlay_path),
            output_path=str(out),
        )
        written = json.loads(out.read_text(encoding="utf-8"))
        assert "modelOverrides" in written["providers"]["anthropic"]

    def test_pi_provider_overlay_does_not_relax_a_discovered_provider(
        self, tmp_path
    ):
        """What must not change: with that overlay present, a stale model on a
        provider lmer discovered still aborts before pi starts."""
        with pytest.raises(pp.ModelNotOfferedError, match="skynet"):
            self._render(
                tmp_path, {"skynet": self._SKYNET},
                requested="skynet/deepseek-v4.1-flash",
                http_get=self._llamacpp_down,
                overlay={"providers": {"anthropic": {"modelOverrides": {
                    "claude-sonnet-4-6": {"contextWindow": 200000}}}}},
            )
        assert not (tmp_path / "models.json").exists()

    def test_unknown_model_on_configured_provider_aborts(self, tmp_path):
        with pytest.raises(pp.ModelNotOfferedError, match="skynet"):
            self._render(
                tmp_path, {"skynet": self._SKYNET},
                requested="skynet/deepseek-v4.1-flash",
                http_get=self._llamacpp_down,
            )
        assert not (tmp_path / "models.json").exists()

    def test_missing_key_unrelated_provider_does_not_abort(self, tmp_path):
        other = {"baseUrl": "https://other.example/v1", "keyEnv": "OTHER_KEY",
                 "keyMissing": True}
        calls: list = []

        def get(url, headers, timeout):
            calls.append(url)
            return {"data": [{"id": "deepseek-v4-flash"}]}

        available, out = self._render(
            tmp_path,
            {"skynet": self._SKYNET, "other": other},
            requested="deepseek-v4-flash",
            http_get=get,
        )
        assert "deepseek-v4-flash" in available
        assert set(json.loads(out.read_text(encoding="utf-8"))["providers"]) == {
            "skynet"
        }
        # A provider with no key is not probed at all.
        assert not any("other.example" in url for url in calls)

    def test_missing_key_provider_in_use_names_the_variable(self, tmp_path):
        other = {"baseUrl": "https://other.example/v1", "keyEnv": "OTHER_KEY",
                 "keyMissing": True}
        with pytest.raises(pp.ProviderConfigError, match="OTHER_KEY is not set"):
            self._render(
                tmp_path,
                {"skynet": self._SKYNET, "other": other},
                requested="other/some-model",
                http_get=self._llamacpp_down,
            )

    def test_rejected_key_of_provider_in_use_is_still_fatal(self, tmp_path):
        """Checked fine by the review and must stay so: a 401 from the provider
        actually in use aborts with the key variable named."""
        def get(url, headers, timeout):
            if "skynet" in url:
                raise pp.ProviderAuthError(401)
            return {"data": [{"id": "qwen3-coder"}]}

        with pytest.raises(pp.ProviderConfigError, match="SKYNET_KEY is set but"):
            self._render(
                tmp_path,
                {"skynet": self._SKYNET, "llamacpp": self._LLAMACPP},
                requested="skynet/deepseek-v4-flash",
                http_get=get,
            )

    def test_rejected_key_of_unrelated_provider_does_not_abort(self, tmp_path):
        def get(url, headers, timeout):
            if "skynet" in url:
                raise pp.ProviderAuthError(401)
            return {"data": [{"id": "qwen3-coder"}]}

        available, _ = self._render(
            tmp_path,
            {"skynet": self._SKYNET, "llamacpp": self._LLAMACPP},
            requested="qwen3-coder",
            http_get=get,
        )
        assert "llamacpp/qwen3-coder" in available

    def test_unset_overlay_key_unrelated_provider_does_not_abort(self, tmp_path):
        overlay = {
            "providers": {
                "openai": {
                    "baseUrl": "https://api.openai.example/v1",
                    "apiKey": "$OPENAI_KEY",
                    "models": [{"id": "gpt-x"}],
                }
            }
        }
        available, out = self._render(
            tmp_path,
            {"skynet": self._SKYNET},
            requested="deepseek-v4-flash",
            http_get=self._llamacpp_down,
            overlay=overlay,
        )
        assert "deepseek-v4-flash" in available
        assert "openai" not in json.loads(out.read_text(encoding="utf-8"))["providers"]

    def test_unset_overlay_key_provider_in_use_aborts(self, tmp_path):
        overlay = {
            "providers": {
                "openai": {
                    "baseUrl": "https://api.openai.example/v1",
                    "apiKey": "$OPENAI_KEY",
                    "models": [{"id": "gpt-x"}],
                }
            }
        }
        with pytest.raises(pp.ProviderConfigError, match="OPENAI_KEY"):
            self._render(
                tmp_path,
                {"skynet": self._SKYNET},
                requested="openai/gpt-x",
                http_get=self._llamacpp_down,
                overlay=overlay,
            )

    def test_overlay_entry_replaces_a_failed_discovery(self, tmp_path):
        """Host entries win: a provider the overlay defines is not a failure
        just because its discovery failed."""
        overlay = {
            "providers": {
                "llamacpp": {
                    "baseUrl": "http://192.168.1.50:8080/v1",
                    "apiKey": "local",
                    "models": [{"id": "qwen3-coder"}],
                }
            }
        }
        available, _ = self._render(
            tmp_path,
            {"skynet": self._SKYNET, "llamacpp": self._LLAMACPP},
            requested="llamacpp/qwen3-coder",
            http_get=self._llamacpp_down,
            overlay=overlay,
        )
        assert "llamacpp/qwen3-coder" in available

    def test_concurrent_reader_never_sees_a_partial_registry(self, tmp_path):
        """A pi child that reads the registry while a sibling launch is
        publishing it must never see a partial or stale file.

        Every pi child of a non-pi parent renders the same shared
        ``models.json``. A publish that truncates that file in place leaves it
        empty inside its own write window — a sibling's pi reads "no custom
        providers" and launches without the host's models.

        The schedule is forced, not raced: if the render opens the target for
        writing (the truncating path), the reader is parked inside that write
        window and reads what its pi would read there. If it never opens the
        target, the publish is happening elsewhere and a rename moves it in;
        the reader then reads under the guard that no rename may land while it
        is reading — the bytes must be the whole previous registry or the whole
        new one, never a half-written file. A truncating publish always fails
        the first branch; a rename that is not atomic fails the second.
        """
        target = tmp_path / "models.json"
        seed = json.dumps({"providers": {"earlier": {"models": [{"id": "old"}]}}})
        target.write_text(seed, encoding="utf-8")

        publishing = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        observed: dict = {}
        original_write_text = Path.write_text
        original_replace = os.replace
        target_str = str(target)

        def scheduled_write_text(self, data, *args, **kwargs):
            # A publish that opens the registry itself is truncating it in
            # place — catch it red-handed and park it before the truncate.
            if str(self) == target_str:
                publishing.set()
                if not release.wait(10):
                    raise TimeoutError("reader never released the writer")
            return original_write_text(self, data, *args, **kwargs)

        def scheduled_replace(src, dst, *args, **kwargs):
            # While the reader is between opening and parsing the registry, no
            # replacement may land — that window is exactly where a partial
            # publish becomes visible.
            if str(dst) == target_str and "error" not in observed \
                    and "document" not in observed:
                raise AssertionError(
                    "registry was replaced while a pi reader was mid-read"
                )
            return original_replace(src, dst, *args, **kwargs)

        def publish():
            try:
                pp.render(
                    environ={
                        pp.PROVIDERS_ENV: json.dumps({
                            "skynet": {
                                "baseUrl": "https://skynet.example/v1",
                                "keyVar": "LMER_PROVIDER_KEY_SKYNET",
                            }
                        }),
                        "LMER_PROVIDER_KEY_SKYNET": "sk-secret",
                    },
                    http_get=lambda url, headers, timeout: {"data": [{"id": "m1"}]},
                    overlay_path=str(tmp_path / "none.json"),
                    output_path=str(target),
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        def read_as_pi_would():
            try:
                observed["document"] = json.loads(target.read_text(encoding="utf-8"))
            except ValueError as exc:
                observed["error"] = str(exc)

        with patch.object(Path, "write_text", scheduled_write_text):
            writer = threading.Thread(target=publish)
            writer.start()
            try:
                if publishing.wait(2):
                    # Truncating publish: read inside its write window.
                    read_as_pi_would()
                else:
                    # Atomic publish: read the target under the
                    # no-replace-mid-read guard.
                    with patch.object(os, "replace", scheduled_replace):
                        read_as_pi_would()
            finally:
                release.set()
                writer.join(timeout=10)

        assert not writer.is_alive()
        assert not errors
        # What the reader got at its scheduled moment: a whole document, and
        # a current one. A parse failure is pi's "no custom providers"; a
        # reader handed the previous file back has lost the same models a
        # moment later.
        assert "error" not in observed, observed["error"]
        assert "m1" in json.dumps(observed["document"]), observed["document"]
        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["providers"]["skynet"]["apiKey"] == "$LMER_PROVIDER_KEY_SKYNET"

    def test_main_reports_failure(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(
            pp.PROVIDERS_ENV,
            json.dumps({"p": {"baseUrl": "https://x/v1", "keyVar": "LMER_PROVIDER_KEY_P"}}),
        )
        monkeypatch.setenv("LMER_PROVIDER_KEY_P", "k")
        monkeypatch.setenv("LMER_LLM_NAME", "p/missing")

        def boom(*args, **kwargs):
            raise pp.ProviderAuthError(401)

        monkeypatch.setattr(pp, "_default_http_get", boom)
        rc = pp.main(["render"])
        assert rc == 1
        assert "rejected" in capsys.readouterr().err


class TestCliForwarding:
    """The host CLI forwards the registry and the key only for a pi session."""

    def _provider_file(self, tmp_path):
        return _write_providers(tmp_path, {
            "skynet": {
                "baseUrl": "https://skynet.example/v1",
                "keyEnv": "SKYNET_KEY",
            }
        })

    def test_pi_forwards_metadata_and_key(self, tmp_path):
        from tests.test_agents_flag import _EXEC_ARGS, _run_main

        captured: dict = {}
        rc = _run_main(
            ["--harness", "pi", *_EXEC_ARGS],
            env_in={
                pp.PROVIDERS_FILE_ENV: str(self._provider_file(tmp_path)),
                "SKYNET_KEY": "sk-secret",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        metadata = json.loads(captured[pp.PROVIDERS_ENV])
        assert metadata["skynet"]["keyVar"] == "LMER_PROVIDER_KEY_SKYNET"
        assert captured["LMER_PROVIDER_KEY_SKYNET"] == "sk-secret"
        assert "sk-secret" not in captured[pp.PROVIDERS_ENV]

    def test_non_pi_harness_does_not_forward_the_registry(self, tmp_path):
        from tests.test_agents_flag import _EXEC_ARGS, _run_main

        captured: dict = {}
        rc = _run_main(
            ["--harness", "claude", *_EXEC_ARGS],
            env_in={
                pp.PROVIDERS_FILE_ENV: str(self._provider_file(tmp_path)),
                "SKYNET_KEY": "sk-secret",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        # The key is generated only by the pi block; the metadata is seeded
        # None (dropped by the transport) for every other harness.
        assert captured.get(pp.PROVIDERS_ENV) is None
        assert "LMER_PROVIDER_KEY_SKYNET" not in captured

    def test_non_pi_parent_forwards_the_registry_for_an_implied_pi_child(
        self, tmp_path
    ):
        """A non-pi session whose ``--agents`` selection routes a child to pi
        must still forward the provider metadata and key: the child execs pi
        directly, so nothing later can generate them."""
        from tests.test_agents_flag import _EXEC_ARGS, _run_main

        presets = tmp_path / "presets.json"
        presets.write_text(
            json.dumps({"pi-review": {"env": {"LMER_HARNESS": "pi"}}}),
            encoding="utf-8",
        )
        captured: dict = {}
        rc = _run_main(
            ["--harness", "claude", "--agents", "pi-review", *_EXEC_ARGS],
            env_in={
                pp.PROVIDERS_FILE_ENV: str(self._provider_file(tmp_path)),
                "SKYNET_KEY": "sk-secret",
                "LMER_PRESETS_FILE": str(presets),
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        metadata = json.loads(captured[pp.PROVIDERS_ENV])
        assert metadata["skynet"]["keyVar"] == "LMER_PROVIDER_KEY_SKYNET"
        assert captured["LMER_PROVIDER_KEY_SKYNET"] == "sk-secret"

    def test_missing_key_does_not_abort_the_launch(self, tmp_path):
        """The host cannot know which provider the session will use, so an
        unset key is the runner's call, not a host-side abort."""
        from tests.test_agents_flag import _EXEC_ARGS, _run_main

        captured: dict = {}
        rc = _run_main(
            ["--harness", "pi", *_EXEC_ARGS],
            env_in={pp.PROVIDERS_FILE_ENV: str(self._provider_file(tmp_path))},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        metadata = json.loads(captured[pp.PROVIDERS_ENV])
        assert metadata["skynet"]["keyMissing"] is True
        assert "LMER_PROVIDER_KEY_SKYNET" not in captured


class TestRunnerWiring:
    """The container-side runner renders and validates before starting pi."""

    def test_runner_invokes_render_and_aborts_on_failure(self):
        script = (
            Path(__file__).parent.parent / "libexec" / "pi-runner.sh"
        ).read_text(encoding="utf-8")
        assert "lmer_cli.pi_providers render" in script
        # A failed render must not fall through to starting pi.
        render_pos = script.index("lmer_cli.pi_providers render")
        exit_pos = script.index("exit 1", render_pos)
        assert exit_pos > render_pos
