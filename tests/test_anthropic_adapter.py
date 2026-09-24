"""Anthropic 原生 adapter 的离线测试(2026-09-24):wire 转换 + SSE 聚合 + 工厂。

不发真实请求:SSE 消费器是纯函数,直接喂事件序列;adapter 的 HTTP 层不在离线面
(api.anthropic.com 连通性与 key 由使用者环境决定,契约靠本套用例锁定)。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otter.models import build_adapter  # noqa: E402
from otter.models.anthropic import (  # noqa: E402
    _consume_sse_events, to_wire_messages, to_wire_tools,
)
from otter.models.openai_compat import OpenAICompatAdapter  # noqa: E402
from otter.models.types import Message, ToolCall, ToolDefinition  # noqa: E402


def test_wire_system_extracted_to_top_level():
    """system 消息不进 messages,拼进顶层 system 参数。"""
    system, wire = to_wire_messages([
        Message(role="system", content="你是 otter"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ])
    assert system == "你是 otter"
    assert [m["role"] for m in wire] == ["user", "assistant"]
    assert all(m["role"] != "system" for m in wire)


def test_wire_assistant_tool_calls_become_tool_use_blocks():
    system, wire = to_wire_messages([
        Message(role="assistant", content=None,
                tool_calls=[ToolCall(id="toolu_01", name="bash", arguments={"cmd": "ls"})]),
    ])
    assert wire[0]["content"] == [{"type": "tool_use", "id": "toolu_01",
                                   "name": "bash", "input": {"cmd": "ls"}}]


def test_wire_tool_messages_merge_into_single_user_turn():
    """OpenAI 的多条 tool 消息 → 聚合成一条 user 消息的 tool_result 块列表(协议要求)。"""
    system, wire = to_wire_messages([
        Message(role="assistant", content=None,
                tool_calls=[ToolCall(id="t1", name="read_file", arguments={}),
                            ToolCall(id="t2", name="grep", arguments={})]),
        Message(role="tool", content="文件内容", tool_call_id="t1", name="read_file"),
        Message(role="tool", content="命中 3 行", tool_call_id="t2", name="grep"),
        Message(role="user", content="继续"),
    ])
    assert [m["role"] for m in wire] == ["assistant", "user", "user"]
    results = [b for b in wire[1]["content"] if b["type"] == "tool_result"]
    assert len(results) == 2  # 两条 tool_result 同一 turn
    assert results[0]["tool_use_id"] == "t1" and results[0]["content"] == "文件内容"
    assert wire[2]["content"] == "继续"


def test_wire_tools_use_input_schema():
    tools = [ToolDefinition(name="bash", description="跑命令", parameters={"type": "object"})]
    assert to_wire_tools(tools) == [{"name": "bash", "description": "跑命令",
                                     "input_schema": {"type": "object"}}]


def _sse(events):
    return [e if isinstance(e, dict) else {"type": e} for e in events]


def test_consume_text_stream():
    deltas = []
    resp = _consume_sse_events(_sse([
        {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
        "message_stop", "ping",  # 噪声事件安全忽略
    ]), on_text_delta=deltas.append)
    assert resp.content == "你好" and not resp.tool_calls
    assert deltas == ["你", "好"]
    assert resp.usage.input_tokens == 25 and resp.usage.output_tokens == 9  # message_delta 覆盖终值
    assert resp.finish_reason == "end_turn"


def test_consume_tool_stream_aggregates_partial_json():
    resp = _consume_sse_events(_sse([
        {"type": "message_start", "message": {"usage": {"input_tokens": 30, "output_tokens": 1}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "先查一下"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use",
                                                                      "id": "toolu_09", "name": "bash"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
                                                              "partial_json": '{"cmd":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
                                                              "partial_json": ' "ls"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}},
    ]))
    assert resp.content == "先查一下"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "toolu_09" and tc.name == "bash"
    assert tc.arguments == {"cmd": "ls"}  # 分片拼接后完整解析
    assert resp.finish_reason == "tool_use"


def test_consume_broken_tool_json_feeds_back_error():
    """坏 JSON 与 openai_compat 同语义:不炸,空参+错误提示回喂让模型自纠。"""
    resp = _consume_sse_events(_sse([
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "x", "name": "bash"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"cmd": "ls"'}},  # 缺右括号
    ]))
    assert "_otter_error" in resp.tool_calls[0].arguments


def test_build_adapter_factory_auto_and_explicit():
    """auto:base_url 含 anthropic.com → 原生;否则兼容层;显式 provider 强制。"""
    from otter.models.anthropic import AnthropicAdapter

    assert isinstance(build_adapter("https://api.anthropic.com", "k", "claude-sonnet-5"),
                      AnthropicAdapter)
    assert isinstance(build_adapter("https://api.deepseek.com", "k", "deepseek-v4-flash"),
                      OpenAICompatAdapter)
    # 显式 anthropic 覆盖 auto 判定(如反代域名场景)
    assert isinstance(build_adapter("https://my-proxy.example", "k", "m", provider="anthropic"),
                      AnthropicAdapter)
    a = build_adapter("https://api.anthropic.com", "k", "m", max_tokens=4096)
    assert a._max_tokens == 4096  # OTTER_MAX_TOKENS 透传(Anthropic 必填项)
