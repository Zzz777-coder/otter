"""v0.3 Trace 分账离线测试(2026-09-24):纯函数聚合 + store 事件读写 + 全链(带反思的 loop)。

运行:.venv/bin/python -m pytest tests/test_trace.py -v
"""

from __future__ import annotations

import asyncio

from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall
from otter.store import Store
from otter.trace import render_ledger, summarize_run_usage
from otter.loop import AgentLoop
from otter.tools.base import Tool, ToolRegistry


def test_summarize_pure_function():
    """三行分账口径:main 含收尾步;summary/reflection 各自独立;total=三者合计。"""
    events = [
        {"type": "MODEL_STARTED", "payload": {"step": 1}},
        {"type": "MODEL_COMPLETED", "payload": {"step": 1, "usage_in": 1000, "usage_out": 50}},
        {"type": "CONTEXT_COMPACTED", "payload": {"compressions": 1, "usage_in": 800, "usage_out": 100}},
        {"type": "MODEL_COMPLETED", "payload": {"step": 2, "usage_in": 1200, "usage_out": 80}},
        {"type": "MEMORY_REFLECTION", "payload": {"result": "(反思新建 M009)", "usage_in": 600, "usage_out": 90}},
        {"type": "MODEL_COMPLETED", "payload": {"step": 30, "finalizing": True, "usage_in": 500, "usage_out": 200}},
        {"type": "TOOL_COMPLETED", "payload": {"name": "echo"}},  # 无关事件不计
    ]
    led = summarize_run_usage(events)
    assert led["main_agent"] == {"input": 2700, "output": 330, "calls": 3}   # 含收尾步
    assert led["context_summary"] == {"input": 800, "output": 100, "calls": 1}
    assert led["memory_reflection"] == {"input": 600, "output": 90, "calls": 1}
    assert led["provider_total"] == {"input": 4100, "output": 520, "calls": 5}
    # 旧库事件(无 usage 键)记 0 不炸
    old = summarize_run_usage([{"type": "MODEL_COMPLETED", "payload": {}}])
    assert old["main_agent"]["input"] == 0 and old["main_agent"]["calls"] == 1
    assert "主模型" in render_ledger(led) and "记忆反思" in render_ledger(led)


def test_store_roundtrip_ledger(tmp_path):
    """store 落事件 → load_run_events → 聚合(异步链)。"""
    import json

    async def main():
        store = Store(tmp_path / "t.db")
        await store.open()
        rid = await store.new_run()
        for t, p in [("MODEL_COMPLETED", {"usage_in": 10, "usage_out": 2}),
                     ("MEMORY_REFLECTION", {"usage_in": 5, "usage_out": 1})]:
            await store.append_event(rid, t, p)
        events = await store.load_run_events(rid)
        led = summarize_run_usage(events)
        assert led["main_agent"]["input"] == 10
        assert led["memory_reflection"]["output"] == 1
        assert await store.latest_run_id() == rid
        await store.close()

    asyncio.run(main())


def test_loop_emits_finalizing_usage_event(tmp_path):
    """收尾步也发 MODEL_COMPLETED(v0.3 前收尾调用零事件,分账漏最后一次)。"""

    class FakeAdapter:
        model = "fake"

        def __init__(self):
            self.calls = 0

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            self.calls += 1
            if self.calls == 1:  # 唯一一步:调工具(进入工具轮)
                return ModelResponse(content=None, tool_calls=[
                    ToolCall(id="c1", name="echo", arguments={"text": "x"})], usage=ModelUsage(100, 10))
            # 后续步给纯文本 → FINAL(不到收尾);为触发收尾,用 max_steps=1:第一步工具轮
            # 结束后循环耗尽 → 收尾步
            return ModelResponse(content="done", usage=ModelUsage(10, 5))

    class EchoTool(Tool):
        name = "echo"
        description = "t"
        parameters = {"type": "object", "properties": {}}

        async def run(self, args):
            return "ok"

    class FakeStore(Store):
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        async def append_event(self, run_id, type_, payload):
            self.events.append((type_, payload))

        async def append_message(self, *a, **k):
            pass

        async def new_run(self):
            return 1

        async def finish_run(self, *a, **k):
            pass

        async def save_checkpoint(self, *a, **k):
            pass

    adapter = FakeAdapter()
    reg = ToolRegistry()
    reg.register(EchoTool())
    store = FakeStore()
    loop = AgentLoop(adapter, reg, store, summarizer=None)
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1, max_steps=1))
    assert result.stop_reason == "max_steps"
    finals = [p for t, p in store.events if t == "MODEL_COMPLETED" and p.get("finalizing")]
    assert finals and finals[0]["usage_in"] == 10  # 收尾事件带 usage
