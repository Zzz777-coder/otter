from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall, ToolDefinition
from otter.models.openai_compat import OpenAICompatAdapter


def build_adapter(base_url: str, api_key: str, model: str, provider: str = "auto",
                  max_tokens: int | None = None):
    """adapter 工厂(2026-09-24 新增):按 provider 选原生实现。

    provider:anthropic=Messages API 原生;openai=兼容层;auto=按 base_url 判定
    (含 anthropic.com → anthropic)。otter 哲学:不用 litellm 类聚合层(说明书选型),
    每家厂商一个显式 adapter,loop 只见 complete_stream。"""
    if provider == "auto":
        provider = "anthropic" if "anthropic.com" in base_url else "openai"
    if provider == "anthropic":
        from otter.models.anthropic import AnthropicAdapter

        return AnthropicAdapter(base_url, api_key, model,
                                max_tokens=max_tokens or 8192)
    return OpenAICompatAdapter(base_url, api_key, model)


__all__ = ["Message", "ModelResponse", "ModelUsage", "ToolCall", "ToolDefinition",
           "OpenAICompatAdapter", "build_adapter"]
