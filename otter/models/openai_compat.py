"""OpenAI 兼容 adapter(DeepSeek / OpenAI / Qwen / Ollama 同一协议)。

设计参考 vesta app/models/providers/openai_compatible.py 的 adapter 思想:
内部类型 ↔ 厂商 wire 格式的转换全部收敛在此层;loop 只见 complete_stream。
流式工具调用的增量聚合(fragment 按 index 拼接 arguments JSON)是本层的关键细节。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall, ToolDefinition

# 流式回调:每个文本增量片段回调一次(REPL 直接打屏)
TextDeltaCallback = Callable[[str], None]


def _to_wire_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """内部 Message 列表 → OpenAI chat.completions 的 messages 参数。"""
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": t.id,
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "arguments": json.dumps(t.arguments, ensure_ascii=False),
                        },
                    }
                    for t in m.tool_calls
                ]
            wire.append(item)
        elif m.role == "tool":
            wire.append(
                {"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content or ""}
            )
        else:  # system / user
            wire.append({"role": m.role, "content": m.content or ""})
    return wire


def _to_wire_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools
    ]


class OpenAICompatAdapter:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0, max_retries=2)

    async def complete_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> ModelResponse:
        """流式补全:文本片段实时回调;工具调用增量聚合为完整 ToolCall 列表。"""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": _to_wire_messages(messages),
            "stream=True 哨兵": None,  # 占位,下面剔除(仅为可读性)
        }
        del kwargs["stream=True 哨兵"]
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}  # 流式也要真实 usage(事后记账)
        if tools:
            kwargs["tools"] = _to_wire_tools(tools)

        content_parts: list[str] = []
        aggregated: dict[int, dict[str, str]] = {}  # index -> {id, name, arguments_json}
        usage = ModelUsage()
        finish_reason: str | None = None

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.usage:
                usage.input_tokens = chunk.usage.prompt_tokens
                usage.output_tokens = chunk.usage.completion_tokens
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                if on_text_delta:
                    on_text_delta(delta.content)
            for frag in delta.tool_calls or []:
                slot = aggregated.setdefault(frag.index, {"id": "", "name": "", "arguments": ""})
                if frag.id:
                    slot["id"] = frag.id
                if frag.function:
                    if frag.function.name:
                        slot["name"] += frag.function.name
                    if frag.function.arguments:
                        slot["arguments"] += frag.function.arguments

        tool_calls: list[ToolCall] = []
        for index in sorted(aggregated):
            slot = aggregated[index]
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
            except json.JSONDecodeError:
                # 弱模型常见病:arguments JSON 不完整。M1 将加"文本工具调用一次性重试"(说明书 5.1),
                # M0 先以空参数+错误提示回喂,让模型下一步自行纠正。
                args = {"_otter_error": f"工具参数 JSON 解析失败,原始串:{slot['arguments'][:200]}"}
            tool_calls.append(ToolCall(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=args))

        return ModelResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )

    async def close(self) -> None:
        await self._client.close()
