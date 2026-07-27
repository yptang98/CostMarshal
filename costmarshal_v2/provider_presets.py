"""Reviewed provider/model presets and end-to-end capability facts.

Presets describe two different things on purpose:

* ``api_capabilities`` are documented by the provider for the selected model.
* ``effective_capabilities`` are the subset CostMarshal's current Codex worker
  can actually deliver end to end.

Routing must use only the effective set.  This prevents a model's marketing
claim from silently becoming an executable capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CAPABILITY_DESCRIPTIONS = {
    "input:text": "Accepts text input.",
    "input:image": "Accepts image input.",
    "input:audio": "Accepts audio input.",
    "input:video": "Accepts video input.",
    "input:document": "Accepts document/file input.",
    "output:text": "Returns text output.",
    "streaming": "Supports streamed responses.",
    "reasoning": "Supports an explicit thinking/reasoning mode.",
    "tool-calling": "Supports function or tool calls.",
    "structured-output": "Supports JSON mode or schema-constrained output.",
    "context-caching": "Supports provider-side prompt/context caching.",
    "long-context": "Has a documented context window of at least 128K tokens.",
    "code": "Is documented or positioned for coding/agent work.",
    "web-search": "Offers a provider-side web-search tool.",
}

# The current worker sends text, optional local images, tool definitions, and
# structured text output through Codex.  Audio/video/document attachment
# transport is deliberately excluded until each is bound into the immutable
# task and worker protocols.  Web search is excluded because generated profiles
# explicitly disable it.
RUNTIME_CAPABILITIES = frozenset(
    {
        "input:text",
        "input:image",
        "output:text",
        "streaming",
        "reasoning",
        "tool-calling",
        "structured-output",
        "context-caching",
        "long-context",
        "code",
    }
)
CHAT_GATEWAY_CAPABILITIES = frozenset(
    {
        "input:text",
        "input:image",
        "input:audio",
        "output:text",
        "streaming",
        "reasoning",
        "tool-calling",
        "structured-output",
        "context-caching",
        "long-context",
        "code",
    }
)
NON_TEXT_INPUT_CAPABILITIES = frozenset(
    {"input:image", "input:audio", "input:video", "input:document"}
)
PRODUCTION_GATEWAY_RUNTIME_ADAPTER = "costmarshal-gateway-v1"


@dataclass(frozen=True)
class ProviderPreset:
    preset_id: str
    provider_id: str
    display_name: str
    base_url: str
    default_model: str
    env_key: str
    wire_api: str | None
    documented_protocols: tuple[str, ...]
    reasoning_effort: str | None
    api_capabilities: tuple[str, ...]
    docs_url: str
    verified_on: str = "2026-07-26"
    note: str = ""

    @property
    def effective_capabilities(self) -> tuple[str, ...]:
        if self.wire_api != "responses":
            return ()
        return tuple(
            capability
            for capability in self.api_capabilities
            if capability in RUNTIME_CAPABILITIES
        )

    @property
    def gateway_compatible(self) -> bool:
        return self.wire_api == "responses" or (
            "openai-chat-completions" in self.documented_protocols
        )

    @property
    def gateway_effective_capabilities(self) -> tuple[str, ...]:
        if not self.gateway_compatible:
            return ()
        supported = (
            RUNTIME_CAPABILITIES
            if self.wire_api == "responses"
            else CHAT_GATEWAY_CAPABILITIES
        )
        return tuple(
            capability
            for capability in self.api_capabilities
            if capability in supported
        )

    @property
    def unavailable_runtime_capabilities(self) -> tuple[str, ...]:
        if self.wire_api != "responses":
            return self.api_capabilities
        return tuple(
            capability
            for capability in self.api_capabilities
            if capability not in RUNTIME_CAPABILITIES
        )

    @property
    def is_api_multimodal(self) -> bool:
        return bool(set(self.api_capabilities) & NON_TEXT_INPUT_CAPABILITIES)

    @property
    def is_runtime_multimodal(self) -> bool:
        return bool(set(self.effective_capabilities) & NON_TEXT_INPUT_CAPABILITIES)

    def to_dict(self) -> dict[str, Any]:
        limitations = [
            f"{capability} is documented by the API but is not transported by the current CostMarshal worker"
            for capability in self.unavailable_runtime_capabilities
            if capability != "web-search"
        ]
        if "web-search" in self.api_capabilities:
            limitations.append(
                "provider-side web search is disabled in generated CostMarshal profiles"
            )
        if self.wire_api != "responses":
            limitations.insert(
                0,
                "the current Codex worker accepts only OpenAI Responses; this preset needs a reviewed Responses gateway",
            )
        return {
            "preset_id": self.preset_id,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "env_key": self.env_key,
            "wire_api": self.wire_api,
            "documented_protocols": list(self.documented_protocols),
            "codex_compatible": self.wire_api == "responses",
            "gateway_compatible": self.gateway_compatible,
            "reasoning_effort": self.reasoning_effort,
            "api_capabilities": list(self.api_capabilities),
            "effective_capabilities": list(self.effective_capabilities),
            "gateway_effective_capabilities": list(
                self.gateway_effective_capabilities
            ),
            "api_multimodal": self.is_api_multimodal,
            "runtime_multimodal": self.is_runtime_multimodal,
            "runtime_limitations": limitations,
            "docs_url": self.docs_url,
            "verified_on": self.verified_on,
            "note": self.note,
        }

    def catalog_provider(
        self,
        *,
        tier: str,
        profile: str,
        model: str | None = None,
        priority: int = 100,
        via_production_gateway: bool = False,
    ) -> dict[str, Any]:
        if via_production_gateway and not self.gateway_compatible:
            raise ValueError(
                f"{self.preset_id} has no protocol supported by the production gateway"
            )
        if not via_production_gateway and self.wire_api != "responses":
            raise ValueError(
                f"{self.preset_id} is not directly compatible with the current "
                "Responses-only Codex worker; use a reviewed Responses gateway"
            )
        if tier not in {"low", "medium", "high"}:
            raise ValueError("tier must be low, medium, or high")
        provider = {
            "provider_id": self.provider_id,
            "tier": tier,
            "profile": profile,
            "model": model or self.default_model,
            "env_key": self.env_key,
            "enabled": True,
            "priority": priority,
            "input_cny_per_1m": None,
            "output_cny_per_1m": None,
            # Only end-to-end capabilities are eligible for hard routing.
            "capabilities": list(
                self.gateway_effective_capabilities
                if via_production_gateway
                else self.effective_capabilities
            ),
        }
        if via_production_gateway:
            provider["runtime_adapter"] = PRODUCTION_GATEWAY_RUNTIME_ADAPTER
        return provider


def _caps(*values: str) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(CAPABILITY_DESCRIPTIONS))
    if unknown:
        raise RuntimeError(f"provider preset contains unknown capabilities: {unknown}")
    return tuple(dict.fromkeys(values))


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "deepseek-v4-flash": ProviderPreset(
        preset_id="deepseek-v4-flash",
        provider_id="deepseek",
        display_name="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
        env_key="DEEPSEEK_API_KEY",
        wire_api=None,
        documented_protocols=("openai-chat-completions", "anthropic-messages"),
        reasoning_effort="low",
        api_capabilities=_caps(
            "input:text",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "structured-output",
            "context-caching",
            "long-context",
            "code",
        ),
        docs_url="https://api-docs.deepseek.com/quick_start/pricing/",
        note="Text-only API. The current Responses-only Codex worker requires a separately reviewed gateway for this provider.",
    ),
    "deepseek-v4-pro": ProviderPreset(
        preset_id="deepseek-v4-pro",
        provider_id="deepseek",
        display_name="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-pro",
        env_key="DEEPSEEK_API_KEY",
        wire_api=None,
        documented_protocols=("openai-chat-completions", "anthropic-messages"),
        reasoning_effort="high",
        api_capabilities=_caps(
            "input:text",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "structured-output",
            "context-caching",
            "long-context",
            "code",
        ),
        docs_url="https://api-docs.deepseek.com/quick_start/pricing/",
        note="Text-only stronger model. The current Responses-only Codex worker requires a separately reviewed gateway.",
    ),
    "kimi-k3": ProviderPreset(
        preset_id="kimi-k3",
        provider_id="kimi",
        display_name="Kimi",
        base_url="https://api.moonshot.ai/v1",
        default_model="kimi-k3",
        env_key="MOONSHOT_API_KEY",
        wire_api=None,
        documented_protocols=("openai-chat-completions",),
        reasoning_effort="high",
        api_capabilities=_caps(
            "input:text",
            "input:image",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "structured-output",
            "context-caching",
            "long-context",
            "code",
        ),
        docs_url="https://platform.kimi.ai/docs/models",
        note="Current visual flagship; a reviewed Responses gateway is required by the current Codex worker.",
    ),
    "kimi-k2.6": ProviderPreset(
        preset_id="kimi-k2.6",
        provider_id="kimi",
        display_name="Kimi",
        base_url="https://api.moonshot.ai/v1",
        default_model="kimi-k2.6",
        env_key="MOONSHOT_API_KEY",
        wire_api=None,
        documented_protocols=("openai-chat-completions",),
        reasoning_effort="high",
        api_capabilities=_caps(
            "input:text",
            "input:image",
            "input:video",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "structured-output",
            "context-caching",
            "long-context",
            "code",
        ),
        docs_url="https://platform.kimi.ai/docs/api/chat",
        note="Multimodal Chat Completions model; a reviewed Responses gateway is required by the current Codex worker.",
    ),
    "longcat-2.0": ProviderPreset(
        preset_id="longcat-2.0",
        provider_id="longcat",
        display_name="LongCat",
        base_url="https://api.longcat.chat/openai/v1",
        default_model="LongCat-2.0",
        env_key="LONGCAT_API_KEY",
        wire_api="responses",
        documented_protocols=("openai-chat-completions", "anthropic-messages"),
        reasoning_effort="low",
        api_capabilities=_caps(
            "input:text",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "long-context",
            "code",
        ),
        docs_url="https://longcat.chat/platform/docs/api/chat.html",
        note="Official docs describe text-only Chat Completions; Responses compatibility was live-verified by this deployment on 2026-07-26.",
    ),
    "mimo-v2.5": ProviderPreset(
        preset_id="mimo-v2.5",
        provider_id="mimo",
        display_name="Xiaomi MiMo",
        base_url="https://api.xiaomimimo.com/v1",
        default_model="mimo-v2.5",
        env_key="MIMO_API_KEY",
        wire_api="responses",
        documented_protocols=(
            "openai-responses",
            "openai-chat-completions",
            "anthropic-messages",
        ),
        reasoning_effort="high",
        api_capabilities=_caps(
            "input:text",
            "input:image",
            "input:audio",
            "input:video",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "structured-output",
            "long-context",
            "code",
            "web-search",
        ),
        docs_url="https://mimo.mi.com/docs/en-US/api/chat/responses",
        note="Native full-modal model; CostMarshal currently transports text and images only.",
    ),
    "mimo-v2.5-pro": ProviderPreset(
        preset_id="mimo-v2.5-pro",
        provider_id="mimo",
        display_name="Xiaomi MiMo",
        base_url="https://api.xiaomimimo.com/v1",
        default_model="mimo-v2.5-pro",
        env_key="MIMO_API_KEY",
        wire_api="responses",
        documented_protocols=(
            "openai-responses",
            "openai-chat-completions",
            "anthropic-messages",
        ),
        reasoning_effort="high",
        api_capabilities=_caps(
            "input:text",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "structured-output",
            "long-context",
            "code",
            "web-search",
        ),
        docs_url="https://mimo.mi.com/docs/en-US/api/chat/responses",
        note="Text-focused high-capability MiMo model for complex reasoning and long-running agents.",
    ),
    "doubao-seed-2.0-lite": ProviderPreset(
        preset_id="doubao-seed-2.0-lite",
        provider_id="doubao",
        display_name="Doubao Ark",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seed-2-0-lite-260428",
        env_key="ARK_API_KEY",
        wire_api="responses",
        documented_protocols=("openai-responses", "openai-chat-completions"),
        reasoning_effort="high",
        api_capabilities=_caps(
            "input:text",
            "input:image",
            "input:audio",
            "input:video",
            "output:text",
            "streaming",
            "reasoning",
            "tool-calling",
            "structured-output",
            "code",
            "web-search",
        ),
        docs_url="https://www.volcengine.com/docs/82379/1795150",
        note="Ark Responses API preset. Verify the dated model ID against the Ark model list before production use.",
    ),
}

PRESET_ALIASES = {
    "deepseek": "deepseek-v4-flash",
    "kimi": "kimi-k3",
    "longcat": "longcat-2.0",
    "mimo": "mimo-v2.5",
    "doubao": "doubao-seed-2.0-lite",
}


def resolve_provider_preset(value: str) -> ProviderPreset:
    preset_id = PRESET_ALIASES.get(value.strip().lower(), value.strip().lower())
    try:
        return PROVIDER_PRESETS[preset_id]
    except KeyError as exc:
        available = ", ".join(sorted((*PROVIDER_PRESETS, *PRESET_ALIASES)))
        raise ValueError(f"unknown provider preset {value!r}; choose one of: {available}") from exc


def provider_presets_payload(preset: str | None = None) -> dict[str, Any]:
    selected = (
        [resolve_provider_preset(preset)]
        if preset
        else [PROVIDER_PRESETS[key] for key in sorted(PROVIDER_PRESETS)]
    )
    return {
        "schema_version": 1,
        "runtime_adapter": {
            "name": "codex-exec",
            "wire_api": "responses",
            "capabilities": sorted(RUNTIME_CAPABILITIES),
            "routing_rule": "provider capabilities are the API/runtime intersection",
        },
        "production_gateway_adapter": {
            "name": PRODUCTION_GATEWAY_RUNTIME_ADAPTER,
            "worker_wire_api": "responses",
            "upstream_wire_apis": ["responses", "chat-completions"],
            "chat_capabilities": sorted(CHAT_GATEWAY_CAPABILITIES),
            "routing_rule": "gateway capabilities are the API/adapter/runtime intersection",
        },
        "capability_vocabulary": dict(sorted(CAPABILITY_DESCRIPTIONS.items())),
        "aliases": dict(sorted(PRESET_ALIASES.items())),
        "presets": [item.to_dict() for item in selected],
    }


__all__ = [
    "CAPABILITY_DESCRIPTIONS",
    "CHAT_GATEWAY_CAPABILITIES",
    "NON_TEXT_INPUT_CAPABILITIES",
    "PRESET_ALIASES",
    "PROVIDER_PRESETS",
    "ProviderPreset",
    "PRODUCTION_GATEWAY_RUNTIME_ADAPTER",
    "RUNTIME_CAPABILITIES",
    "provider_presets_payload",
    "resolve_provider_preset",
]
