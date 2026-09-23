"""离线单元测试:全部用 fake adapter/store,不发任何真实请求。

测试策略为离线测试取向(说明书第 7 章:测试全离线)。
运行:python -m pytest tests/ -v(测试体用 asyncio.run 手动驱动,不依赖插件)
"""

from __future__ import annotations

import asyncio

from otter.loop import AgentLoop, STOP_FINAL, STOP_MAX_STEPS, STOP_REPEATED
from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall, ToolDefinition
from otter.tools.base import Tool, ToolRegistry


class FakeAdapter:
    """按脚本回放的假 adapter:依次返回预设 ModelResponse。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []  # 记录每次请求的 tools 是否为空(验证"零工具收尾")

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        resp = self.script.pop(0)
        self.calls.append({"had_tools": bool(tools), "messages_tail": messages[-1].role})
        if on_text_delta and resp.content:
            on_text_delta(resp.content)
        return resp


class FakeStore:
    """内存版 Store,只记录调用供断言。"""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.messages: list[Message] = []
        self.run_status: tuple[str, str] | None = None

    async def new_run(self):
        return 1

    async def finish_run(self, run_id, status, stop_reason):
        self.run_status = (status, stop_reason)

    async def append_message(self, msg, seq, run_id, conversation_id=None):  # v5:签名跟随 loop 透传
        self.messages.append(msg)

    async def save_checkpoint(self, *a, **k):
        pass  # M2:loop 新增落档调用,fake 兼容

    async def append_event(self, run_id, type_, payload):
        self.events.append(type_)


class EchoTool(Tool):
    name = "echo"
    description = "回显文本(测试用)"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, args):
        return f"echo: {args.get('text', '')}"


def _loop(adapter) -> tuple[AgentLoop, FakeStore]:
    store = FakeStore()
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentLoop(adapter, registry, store), store


def test_happy_path_tool_then_final():
    """模型先调一次工具,再给最终答案 → STOP_FINAL,工具结果已回喂进历史。"""
    adapter = FakeAdapter([
        # 修正(2026-09-19):两次响应都提供 usage——"未知传播"语义下(见 test_usage_none_semantics_preserved),
        # 首次缺 usage 会令 Run 总和恒为 None,原断言 10/5 永不成立;改为验证跨调用累加
        ModelResponse(content=None, tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})], usage=ModelUsage(100, 20)),
        ModelResponse(content="完成:echo 返回了 hi", usage=ModelUsage(10, 5)),
    ])
    loop, store = _loop(adapter)
    result = asyncio.run(loop.run([], Message(role="user", content="测试"), run_id=1))
    assert result.ok and result.stop_reason == STOP_FINAL
    assert result.tool_calls == 1 and result.model_calls == 2
    assert result.usage.input_tokens == 110 and result.usage.output_tokens == 25
    # 消息序列:user → assistant(带tool_calls) → tool → assistant(最终)
    roles = [m.role for m in store.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert store.messages[2].tool_call_id == "c1"
    assert store.run_status == ("completed", STOP_FINAL)


def test_max_steps_triggers_zero_tool_finalization():
    """步数用尽 → 收尾专用步必须以零工具表调用(结构上杜绝再调工具)。"""
    script = [
        ModelResponse(content=None, tool_calls=[ToolCall(id=f"c{i}", name="echo", arguments={"text": str(i)})])
        for i in range(3)  # max_steps=3,每步都调工具,故意耗尽
    ] + [ModelResponse(content="总结:做了 3 次 echo")]  # 收尾步响应
    adapter = FakeAdapter(script)
    loop, store = _loop(adapter)
    result = asyncio.run(loop.run([], Message(role="user", content="测试"), run_id=1, max_steps=3))
    assert result.stop_reason == STOP_MAX_STEPS and result.model_calls == 4
    assert adapter.calls[-1]["had_tools"] is False  # 收尾请求不带工具表
    assert store.run_status == ("completed", STOP_MAX_STEPS)


def test_repeated_tool_call_termination():
    """同签名调用累计 3 次 → STOP_REPEATED 兜底终止。"""
    same = [ToolCall(id=f"c{i}", name="echo", arguments={"text": "same"}) for i in range(3)]
    adapter = FakeAdapter([
        ModelResponse(content=None, tool_calls=[same[0]]),
        ModelResponse(content=None, tool_calls=[same[1]]),
        ModelResponse(content=None, tool_calls=[same[2]]),
        ModelResponse(content="不应到达这里"),
    ])
    loop, store = _loop(adapter)
    result = asyncio.run(loop.run([], Message(role="user", content="测试"), run_id=1, max_steps=10))
    assert result.stop_reason == STOP_REPEATED and not result.ok
    assert store.run_status[1] == STOP_REPEATED


def test_usage_none_semantics_preserved():
    """usage 未知(None)时,累加结果仍为 None,不伪装成 0(记账语义)。"""
    adapter = FakeAdapter([
        ModelResponse(content=None, tool_calls=[ToolCall(id="c", name="echo", arguments={})], usage=ModelUsage(None, None)),
        ModelResponse(content="done", usage=ModelUsage(10, 5)),
    ])
    loop, _ = _loop(adapter)
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1))
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
