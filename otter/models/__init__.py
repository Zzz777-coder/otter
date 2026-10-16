from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall, ToolDefinition
from otter.models.openai_compat import OpenAICompatAdapter
from otter.models.retry import ModelRetryExhausted, RetryingAdapter, complete_stream_with_retry


def build_adapter(base_url: str, api_key: str, model: str, provider: str = "auto",
                  max_tokens: int | None = None,
                  temperature: float | None = None, top_p: float | None = None):
    """adapter 工厂(2026-09-24 新增):按 provider 选原生实现。

    provider:anthropic=Messages API 原生;openai=兼容层;auto=按 base_url 判定
    (含 anthropic.com → anthropic)。otter 哲学:不用 litellm 类聚合层(说明书选型),
    每家厂商一个显式 adapter,loop 只见 complete_stream。

    2026-10-02:工厂出口统一套 RetryingAdapter——loop/压缩/反思/技能提炼所有模型
    调用点对 429/5xx/网络抖动获得一致的指数退避重试(此前 openai 系靠 SDK 内建
    两次、anthropic 系零重试,行为分裂且长任务一次瞬态失败即全丢)。

    2026-10-13 新增:temperature/top_p 透传(None=不发送,走服务端默认;
    OTTER_TEMPERATURE/OTTER_TOP_P 显式配置时经 engine/gui 装配传入)。"""
    if provider == "auto":
        provider = "anthropic" if "anthropic.com" in base_url else "openai"
    if provider == "anthropic":
        from otter.models.anthropic import AnthropicAdapter

        return RetryingAdapter(AnthropicAdapter(base_url, api_key, model,
                                                max_tokens=max_tokens or 8192,
                                                temperature=temperature, top_p=top_p))
    return RetryingAdapter(OpenAICompatAdapter(base_url, api_key, model,
                                               temperature=temperature, top_p=top_p))


__all__ = ["Message", "ModelResponse", "ModelUsage", "ToolCall", "ToolDefinition",
           "OpenAICompatAdapter", "ModelRetryExhausted", "RetryingAdapter",
           "complete_stream_with_retry", "build_adapter"]
