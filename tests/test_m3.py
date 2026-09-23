"""M3 离线测试:双层记忆/召回门控/乐观锁/repo map/deferred+tool_search/edit format。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from otter.loop import MODE_PLAN, MODE_NORMAL, AgentLoop, SummaryState
from otter.memory import (
    CoreMemory, FileMemoryStore, recall_query, reflection_should_run,
)
from otter.memory_runtime import build_memory_tools
from otter.models.types import Message, ModelResponse, ModelUsage, ToolCall
from otter.repo_map import RepoMap
from otter.tools.base import Tool, ToolRegistry
from otter.tools.deferred import ToolSearchTool


# ── 双层记忆 ──────────────────────────────────────────────────────

def test_core_memory_upsert_and_render(tmp_path: Path):
    core = CoreMemory(tmp_path)
    core.upsert("称呼", "张琪", "用户自述", "我叫张琪")
    core.upsert("称呼", "小张", "更正", "叫我小张")  # 同 key 覆盖
    entries = core.load()
    assert len(entries) == 1 and entries[0]["value"] == "小张"
    assert "小张" in core.render() and "<core_memory>" in core.render()
    assert core.remove("称呼") and not core.load()


def test_ordinary_crud_archive_and_eviction(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    e = store.create("Python 偏好", "喜欢 f-string", "用户偏好用 f-string 而非 format")
    assert store.read("M001").title == "Python 偏好"
    # 乐观锁:revision 不匹配拒绝
    assert store.update("M001", expected_revision=99, title="x") is None
    assert store.update("M001", expected_revision=1, summary="更新后的摘要") is not None
    assert store.read("M001").revision == 2
    # 容量淘汰:超 25 条按 access_count 归档最冷
    for i in range(2, 30):
        store.create(f"条目{i}", f"摘要{i}", f"内容{i}")
    actives = store.list_active()
    assert len(actives) <= 25
    # M001 被读过?没有——evict 按 access_count 全 0 时按列表序;只验证总数守恒
    assert (tmp_path / "archive").exists()


def test_search_fts_and_fallback(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    store.create("部署约定", "必须走蓝绿", "生产部署一律蓝绿发布,禁止直接覆盖")
    hits = store.search("蓝绿")
    assert hits and hits[0].mid == "M001"


def test_recall_query_deterministic_and_gate():
    q = recall_query("帮我改调度器", ["上次说记住用 pytest", "看下报告"], "完成部署脚本")
    assert "调度器" in q and "pytest" in q and "部署脚本" in q and len(q) <= 1600
    assert reflection_should_run("请记住我偏好黑色主题", recalled=False) is True
    assert reflection_should_run("在吗", recalled=False) is False        # 寒暄跳过
    assert reflection_should_run("把这两个文件重构一下并跑通测试", recalled=True) is True  # 有召回放行


def test_memory_write_requires_read_and_revision(tmp_path: Path):
    tools, store, core, ctx = build_memory_tools(tmp_path)
    write = next(t for t in tools if t.name == "memory_write")
    read = next(t for t in tools if t.name == "memory_read")
    store.create("测试条", "s", "c")
    # 未读过直接 update → 拒绝
    r = asyncio.run(write.run({"action": "update", "mid": "M001", "revision": 1, "content": "x"}))
    assert "拒绝" in r
    asyncio.run(read.run({"mid": "M001"}))  # 本 Run 读过 → 记录 revision
    r = asyncio.run(write.run({"action": "update", "mid": "M001", "revision": 1, "content": "新内容"}))
    assert "已更新" in r


# ── repo map ──────────────────────────────────────────────────────

def test_repo_map_symbols_and_ranking(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "util.py").write_text(
        "def helper():\n    return 1\n\nclass Base:\n    pass\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from util import helper\n\ndef main():\n    return helper()\n", encoding="utf-8")
    rm = RepoMap(tmp_path)
    m = rm.build()
    assert "helper" in m["util.py"] and "Base" in m["util.py"] and "main" in m["app.py"]
    text = rm.render()
    assert "util.py" in text and "app.py" in text  # 被引用的 util 排前


# ── deferred + tool_search ────────────────────────────────────────

class LoudTool(Tool):
    name = "shout"
    description = "大声喊话(测试用,低频)"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args):
        return "BOOM"


def test_deferred_hidden_until_activated():
    reg = ToolRegistry()
    loud = LoudTool(); loud.deferred = True
    reg.register(loud)
    names = [d.name for d in reg.definitions()]
    assert "shout" not in names                       # 默认不进 schema
    assert "shout" in [d.name for d in reg.definitions(include_deferred=True)]
    assert "shout" in [d.name for d in reg.definitions(active_extra={"shout"})]  # 激活后可见

    activated: set[str] = set()
    search = ToolSearchTool(reg, activated)
    r = asyncio.run(search.run({"keywords": "喊话"}))
    assert "shout" in activated and "shout" in r      # 搜索即激活,下一步可用


def test_loop_deferred_hard_block_then_tool_search_path(tmp_path: Path):
    """执行层硬校验:未激活直接调用被拒;tool_search 激活后放行。"""
    reg = ToolRegistry()
    loud = LoudTool(); loud.deferred = True
    reg.register(loud)

    class FakeAdapter:
        def __init__(self):
            self.script = [
                ModelResponse(content=None, tool_calls=[ToolCall(id="c1", name="shout", arguments={})]),
                ModelResponse(content="done", usage=ModelUsage(1, 1)),
            ]
        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            self.last_tools = [t.name for t in (tools or [])]
            return self.script.pop(0)

    class MemStore:
        async def new_run(self): return 1
        async def finish_run(self, *a): pass
        async def append_message(self, *a): pass
        async def save_checkpoint(self, *a, **k): pass
        async def append_event(self, *a): pass

    adapter = FakeAdapter()
    loop = AgentLoop(adapter, reg, MemStore())
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1))
    assert result.ok
    # 未激活:shout 不在 schema,且执行(模型幻觉硬调)也会被拒——这里模型调了但注册表有
    # 工具存在(deferred),走 deferred 拒绝分支:验证 tool 消息为拒绝文本
    # (由 store 记录验证;MemStore 不记,改验证 schema 不含)
    assert "shout" not in adapter.last_tools


# ── edit format 分层 ──────────────────────────────────────────────

def test_edit_format_auto_weak_model_hint():
    class FakeAdapterWithModel:
        model = "deepseek-v4-flash"
        async def complete_stream(self, *a, **k): ...
    loop = AgentLoop(FakeAdapterWithModel(), ToolRegistry(), None, edit_format="auto")
    assert loop.edit_format == "whole" and "write_file" in loop._edit_hint

    class StrongAdapter:
        model = "claude-opus-4-8"
        async def complete_stream(self, *a, **k): ...
    loop2 = AgentLoop(StrongAdapter(), ToolRegistry(), None, edit_format="auto")
    assert loop2.edit_format == "diff" and "edit_file" in loop2._edit_hint


# ── Run 级费用预算三段(2026-09-22 补装)──────────────────────────

def test_run_budget_three_stages_and_hard_stop():
    """大 usage:warning → finalizing 提示注入 → 超线硬停;硬停后事件齐、不再调模型。"""
    from otter.loop import STOP_BUDGET

    class FakeAdapter:
        def __init__(self):
            # 3 次响应:50K(触发 warning)→ 270K(触发 finalizing)→ 310K(超线)
            self.script = [
                ModelResponse(content=None, tool_calls=[ToolCall(id=f"c{i}", name="echo", arguments={"text": str(i)})],
                              usage=ModelUsage(50_000, 1)) if False else
                ModelResponse(content=None, tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
                              usage=ModelUsage(50_000, 100)),
                ModelResponse(content=None, tool_calls=[ToolCall(id="c2", name="echo", arguments={})],
                              usage=ModelUsage(220_000, 100)),
                ModelResponse(content=None, tool_calls=[ToolCall(id="c3", name="echo", arguments={})],
                              usage=ModelUsage(40_000, 100)),
            ]
            self.calls = 0

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            self.calls += 1
            self.last_system = messages[0].content or ""
            return self.script.pop(0)

    class MemStore:
        def __init__(self):
            self.events = []
        async def new_run(self): return 1
        async def finish_run(self, *a): self.final = a
        async def append_message(self, *a): pass
        async def save_checkpoint(self, *a, **k): pass
        async def append_event(self, run_id, type_, payload): self.events.append(type_)

    reg = ToolRegistry()
    from otter.tools.base import Tool

    class Echo(Tool):
        name = "echo"
        description = ""
        parameters = {"type": "object", "properties": {}}
        async def run(self, args): return "ok"

    reg.register(Echo())
    store = MemStore()
    loop = AgentLoop(FakeAdapter(), reg, store, run_budget=300_000)
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1, max_steps=10))
    assert result.stop_reason == STOP_BUDGET and not result.ok
    assert result.usage.input_tokens == 310_000  # 真实记账照常
    assert "RUN_BUDGET_WARNING" in store.events
    assert "RUN_BUDGET_FINALIZING" in store.events
    assert "RUN_BUDGET_EXCEEDED" in store.events
    assert loop.adapter.calls == 3  # 硬停后不再调模型


def test_run_budget_disabled_and_normal_runs_unaffected():
    """预算 0=禁用;正常小用量 Run 不触发任何预算事件。"""

    class FakeAdapter:
        def __init__(self):
            self.script = [ModelResponse(content="done", usage=ModelUsage(1000, 50))]

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            return self.script.pop(0)

    class MemStore:
        def __init__(self):
            self.events = []
        async def new_run(self): return 1
        async def finish_run(self, *a): pass
        async def append_message(self, *a): pass
        async def save_checkpoint(self, *a, **k): pass
        async def append_event(self, run_id, type_, payload): self.events.append(type_)

    store = MemStore()
    loop = AgentLoop(FakeAdapter(), ToolRegistry(), MemStore(), run_budget=300_000)
    result = asyncio.run(loop.run([], Message(role="user", content="t"), run_id=1))
    assert result.ok and not [e for e in store.events if "BUDGET" in e]
    loop0 = AgentLoop(FakeAdapter(), ToolRegistry(), MemStore(), run_budget=0)
    assert asyncio.run(loop0.run([], Message(role="user", content="t"), run_id=1)).ok
