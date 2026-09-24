"""Anthropic Messages API 原生 adapter(2026-09-24 新增)。

与 openai_compat 同一契约(complete_stream),wire 差异全部收敛在此层:
- system 消息 → 顶层 system 参数(Anthropic 不收 messages 里的 system role);
- assistant.tool_calls → content 块 tool_use;role=tool 消息 → 合并进紧随的
  user turn 的 tool_result 块(Anthropic 要求 tool_result 挂在 user 消息上,
  OpenAI 的"每个 tool_call 一条 tool 消息"须聚合为一条 user);
- ToolDefinition.parameters → tools[].input_schema(字段名不同);
- max_tokens 必填(OpenAI 可选),取 OTTER_MAX_TOKENS(默认 8192);
- 流式 SSE:message_start(usage.input)/content_block_start(tool_use 起)
  /content_block_delta(text_delta | input_json_delta)/message_delta(终态
  usage.output + stop_reason);ping/error 事件按需忽略或抛错。

SSE 事件消费拆为纯函数 _consume_sse_events(吃完整事件序列→ModelResponse),
离线测试零 mock;complete_stream 只负责取行喂它。
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx

from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall, ToolDefinition

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192


def to_wire_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """内部 Message 列表 → (system, Anthropic messages)。

    返回二元组:system 为拼接后的顶层系统提示(无则空串);messages 为 wire 列表。
    连续 tool 消息聚合成一个 user turn(tool_result 块列表)——协议要求。"""
    system_parts: list[str] = []
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for t in m.tool_calls:
                blocks.append({"type": "tool_use", "id": t.id, "name": t.name,
                               "input": t.arguments or {}})
            wire.append({"role": "assistant", "content": blocks if blocks else (m.content or "")})
        elif m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id or "",
                     "content": m.content or ""}
            # 并入紧邻的前一条 user 消息(由 tool 转来,content 是块列表);
            # 前一条是普通 user(字符串 content,理论不出现的序列)则独立成 turn,保协议合法
            if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                wire[-1]["content"].append(block)
            else:
                wire.append({"role": "user", "content": [block]})
        else:  # user
            wire.append({"role": "user", "content": m.content or ""})
    return "\n\n".join(system_parts), wire


def to_wire_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """内部 ToolDefinition → Anthropic tools 参数(parameters 改名 input_schema)。"""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]


def _consume_sse_events(events: list[dict[str, Any]], on_text_delta=None) -> ModelResponse:
    """纯函数:消费完整 SSE data 事件序列 → ModelResponse(离线可测)。

    文本:text_delta 累积并实时回调;工具:content_block_start(tool_use) 记 id/name,
    input_json_delta 按 index 拼 partial_json;usage:message_start 给 input,
    message_delta 的 output 覆盖终值;stop_reason 取 message_delta。"""
    content_parts: list[str] = []
    usage = ModelUsage()
    stop_reason: str | None = None
    blocks: dict[int, dict[str, str]] = {}  # index -> {id, name, json}

    for ev in events:
        etype = ev.get("type")
        if etype == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            usage.input_tokens = u.get("input_tokens")
            usage.output_tokens = u.get("output_tokens")
        elif etype == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                blocks[ev.get("index", 0)] = {"id": cb.get("id", ""),
                                             "name": cb.get("name", ""), "json": ""}
        elif etype == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta":
                text = d.get("text", "")
                content_parts.append(text)
                if on_text_delta:
                    on_text_delta(text)
            elif d.get("type") == "input_json_delta":
                idx = ev.get("index", 0)
                slot = blocks.setdefault(idx, {"id": "", "name": "", "json": ""})
                slot["json"] += d.get("partial_json", "")
        elif etype == "message_delta":
            u = ev.get("usage") or {}
            if u.get("output_tokens") is not None:
                usage.output_tokens = u["output_tokens"]
            sr = (ev.get("delta") or {}).get("stop_reason")
            if sr:
                stop_reason = sr

    tool_calls: list[ToolCall] = []
    for idx in sorted(blocks):
        slot = blocks[idx]
        try:
            args = json.loads(slot["json"]) if slot["json"].strip() else {}
        except json.JSONDecodeError:
            # 与 openai_compat 同语义:坏 JSON 不炸,空参+错误提示回喂让模型自纠
            args = {"_otter_error": f"工具参数 JSON 解析失败,原始串:{slot['json'][:200]}"}
        tool_calls.append(ToolCall(id=slot["id"] or f"toolu_{idx}", name=slot["name"], arguments=args))

    return ModelResponse(content="".join(content_parts) or None, tool_calls=tool_calls,
                         usage=usage, finish_reason=stop_reason)


class AnthropicAdapter:
    """Messages API 原生实现(2026-09-24):之前 Anthropic 系模型只能借 OpenAI 兼容层转译。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.model = model
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=120.0)

    async def complete_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        on_text_delta=None,
    ) -> ModelResponse:
        system, wire_msgs = to_wire_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,  # Anthropic 必填
            "messages": wire_msgs,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = to_wire_tools(tools)

        events: list[dict[str, Any]] = []
        async with self._client.stream(
            "POST", f"{self._base}/v1/messages",
            headers={"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            json=body,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # event:/ping/空行不携带 data 负载
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                events.append(json.loads(payload))
        return _consume_sse_events(events, on_text_delta)

    async def close(self) -> None:
        await self._client.aclose()
