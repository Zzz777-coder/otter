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
        # v0.3:存 (type, payload) 元组——重试/取消/分账用例需要断言事件字段
        self.events.append((type_, payload))


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


# ── 2026-09-24 运行中取消:协作检查点 ─────────────────────────────

def test_cancel_before_first_step():
    """Run 前取消事件已置位 → 第一步检查点即收尾,零模型调用,终态 cancelled。"""
    from otter.loop import STOP_CANCELLED

    adapter = FakeAdapter([ModelResponse(content="不应被调用", usage=ModelUsage(1, 1))])
    loop, store = _loop(adapter)
    ev = asyncio.Event()
    ev.set()  # 预置:run 一进 Step① 检查点即取消
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1,
                                  cancel_event=ev))
    assert result.ok is False and result.stop_reason == STOP_CANCELLED
    assert result.model_calls == 0 and result.tool_calls == 0
    assert store.run_status == ("cancelled", STOP_CANCELLED)
    assert any(t == "RUN_CANCELLED" for t, _ in store.events)


def test_cancel_between_tools_skips_rest():
    """工具循环内取消:第一个工具执行后置位 → 第二个工具前检查点拦截,不再烧模型。"""
    from otter.loop import STOP_CANCELLED

    # 两个 echo 调用同一响应分岔(一次模型响应带两个 tool_calls)
    adapter = FakeAdapter([
        ModelResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="echo", arguments={"text": "a"}),
            ToolCall(id="c2", name="echo", arguments={"text": "b"}),
        ], usage=ModelUsage(10, 2)),
        ModelResponse(content="完成", usage=ModelUsage(5, 1)),  # 不应被消费
    ])
    loop, store = _loop(adapter)
    ev = asyncio.Event()

    # 借 evidence 归档钩子不可行——直接在 EchoTool 上挂 set?更简单:用 on_event 回调
    # 在第一个 TOOL_COMPLETED 后置位取消(模拟"工具跑完用户立刻点停止")
    async def on_event(type_, payload):
        if type_ == "TOOL_COMPLETED":
            ev.set()

    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1,
                                  cancel_event=ev, on_event=on_event))
    assert result.ok is False and result.stop_reason == STOP_CANCELLED
    assert result.tool_calls == 1          # c1 执行了,c2 被检查点拦下
    assert result.model_calls == 1         # 第二次模型调用没有发生
    assert len(adapter.script) == 1        # 收尾脚本未被消费
    assert store.run_status == ("cancelled", STOP_CANCELLED)
    # 协议安全:取消时历史尾部为 tool 结果(c1),不是孤立 tool_calls
    assert store.messages[-1].role == "tool" and store.messages[-1].tool_call_id == "c1"


# ── v0.3 健壮性话术族:空响应/协议文本重试 + Closing 第四段 ──────────

def test_empty_response_retry_then_final():
    """空响应(无文本无工具)→ 注入重试消息再跑一圈;仍空则按 FINAL 收(不死循环)。"""
    adapter = FakeAdapter([
        ModelResponse(content="", usage=ModelUsage(10, 1)),      # 第一次:空
        ModelResponse(content="这次有答案了", usage=ModelUsage(10, 1)),  # 重试后:正常
    ])
    loop, store = _loop(adapter)
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1))
    assert result.ok and result.stop_reason == STOP_FINAL
    assert result.final_text == "这次有答案了"
    assert any(t == "MODEL_EMPTY_RETRY" for t, _ in store.events)
    # 消息序列:空 assistant 后紧跟 user 重试消息(协议合法)
    assert [m.role for m in store.messages] == ["user", "assistant", "user", "assistant"]


def test_textual_tool_call_retry():
    """模型把 <tool_calls> 当正文吐出 → 注入重试话术;恢复结构化或正常文本即收。"""
    from otter.loop import looks_like_textual_tool_call

    assert looks_like_textual_tool_call("看这个 <tool_calls>{...}</TOOL_CALLS>")
    assert not looks_like_textual_tool_call("正常回答")
    adapter = FakeAdapter([
        ModelResponse(content="<tool_calls>{\"name\":\"echo\"}</tool_calls>", usage=ModelUsage(10, 1)),
        ModelResponse(content="改好了,直接给结论", usage=ModelUsage(10, 1)),
    ])
    loop, store = _loop(adapter)
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1))
    assert result.ok and result.final_text == "改好了,直接给结论"
    assert any(t == "MODEL_TEXTUAL_RETRY" for t, _ in store.events)


def test_budget_closing_fourth_stage():
    """85% finalizing → 92% closing(工具面收窄);分界事件各发一次。"""
    adapter = FakeAdapter([
        # 一次大调用直接跳到 85%+ 段(finalizing)
        ModelResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="echo", arguments={"text": "x"})], usage=ModelUsage(880, 20)),
        # 第二次到 92%+(closing)
        ModelResponse(content=None, tool_calls=[
            ToolCall(id="c2", name="echo", arguments={"text": "y"})], usage=ModelUsage(60, 10)),
        ModelResponse(content="完成", usage=ModelUsage(5, 1)),
    ])
    loop, store = _loop(adapter)
    # run_budget=1000:880/1000=88% → finalizing;960 → closing
    loop.run_budget = 1000
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1))
    assert result.ok
    assert any(t == "RUN_BUDGET_FINALIZING" for t, _ in store.events)
    assert any(t == "RUN_BUDGET_CLOSING" for t, _ in store.events)


# ── v0.3 多模型角色:反思走 reflection_adapter ─────────────────────

def test_reflection_uses_reflection_adapter(tmp_path):
    """反思调用落在 reflection_adapter(便宜模型),主 adapter 只跑主轮;分账事件带 usage。"""
    from otter.memory import CoreMemory, FileMemoryStore

    class ReflAdapter(FakeAdapter):
        def __init__(self):
            super().__init__([
                ModelResponse(content='{"action":"none"}', usage=ModelUsage(77, 7)),
            ])

    main_adapter = FakeAdapter([
        ModelResponse(content="好的,已经了解你的偏好了。", usage=ModelUsage(100, 20)),
    ])
    refl = ReflAdapter()
    registry = ToolRegistry()
    loop = AgentLoop(main_adapter, registry, FakeStore(),
                     memory_bundle=(FileMemoryStore(tmp_path / "m"), CoreMemory(tmp_path / "m"),
                                    {"reads": {}, "run_id": None}),
                     reflection_adapter=refl)
    # user 消息带长期信号词("记住")→ 触发 Run 后反思
    result = asyncio.run(loop.run([], Message(role="user", content="请记住我的偏好是简洁回复风格"),
                                  run_id=1))
    assert result.ok
    assert len(main_adapter.script) == 0 and len(refl.script) == 0  # 两边各自消费完
    # 反思 usage 进事件(分账"记忆反思"行数据源)
    assert any(t == "MEMORY_REFLECTION" and p.get("usage_in") == 77
               for t, p in loop.store.events)
