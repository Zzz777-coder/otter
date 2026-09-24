"""Plan Mode v2 离线测试:计划契约/落盘/plan_context 注入/采纳重跑闭环。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otter.loop import MODE_NORMAL, MODE_PLAN, AgentLoop
from otter.models.types import Message, ModelResponse, ModelUsage
from otter.plans import list_plans, load_plan, plan_is_valid, save_plan
from otter.tools.base import ToolRegistry
from otter.tools.builtin import ReadFileTool


class PlanAdapter:
    """回放:记录每次请求的 system;按序返回工具调用→计划→执行终稿。"""

    def __init__(self, script):
        self.script = list(script)
        self.systems: list[str] = []
        self.tools_seen: list[list[str]] = []

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.systems.append(messages[0].content or "")
        self.tools_seen.append(sorted(t.name for t in (tools or [])))
        resp = self.script.pop(0)
        if on_text_delta and resp.content:
            on_text_delta(resp.content)
        return resp


class MemStore:
    def __init__(self):
        self.events = []

    async def new_run(self):
        return 1

    async def finish_run(self, *a):
        pass

    async def append_message(self, *a):
        pass

    async def save_checkpoint(self, *a, **k):
        pass

    async def append_event(self, run_id, type_, payload):
        self.events.append((type_, payload))


VALID_PLAN = "## 目标\n加减法\n## 现状与前置\n已有 demo.py\n## 步骤\n1. read demo.py\n2. edit_file 加函数\n## 验收标准\ntest 通过\n## 风险\n无"


def test_plan_contract_validation_and_save(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert plan_is_valid(VALID_PLAN) is True
    assert plan_is_valid("我觉得应该这么做,首先……") is False  # 无 ## 步骤

    p = save_plan("加功能", VALID_PLAN)
    assert p.name == "plan-001.md" and "task: 加功能" in p.read_text(encoding="utf-8")
    save_plan("第二份", VALID_PLAN)
    assert [r["file"] for r in list_plans()] == ["plan-002.md", "plan-001.md"]
    body = load_plan("plan-001")  # 按序号
    assert body is not None and body.startswith("## 目标")  # front matter 已剥离


def test_plan_mode_directive_and_result_event(tmp_path: Path, monkeypatch):
    """PLAN 模式:system 含产品经理指令;成功后发 PLAN_RESULT(valid)+ 落盘。"""
    monkeypatch.chdir(tmp_path)
    adapter = PlanAdapter([
        ModelResponse(content=VALID_PLAN, usage=ModelUsage(10, 5)),
    ])
    reg = ToolRegistry()
    reg.register(ReadFileTool())
    store = MemStore()
    loop = AgentLoop(adapter, reg, store)
    result = asyncio.run(loop.run([], Message(role="user", content="加功能"), run_id=1, mode=MODE_PLAN))
    assert result.ok
    assert "PLAN MODE" in adapter.systems[0] and "## 步骤" in adapter.systems[0]  # 指令注入
    assert "write_file" not in adapter.tools_seen[0]  # 只读白名单
    plan_events = [e for e in store.events if e[0] == "PLAN_RESULT"]
    assert plan_events and plan_events[0][1]["valid"] is True
    assert (tmp_path / ".otter/plans/plan-001.md").is_file()  # 自动落盘


def test_approved_plan_context_injected_on_execution_run(tmp_path: Path, monkeypatch):
    """采纳后执行跑:plan_context 出现在 system 的 <approved_plan> 块,模式为普通。"""
    monkeypatch.chdir(tmp_path)
    adapter = PlanAdapter([
        ModelResponse(content="已完成:按计划加了函数", usage=ModelUsage(10, 5)),
    ])
    reg = ToolRegistry()
    reg.register(ReadFileTool())
    loop = AgentLoop(adapter, reg, MemStore())
    result = asyncio.run(loop.run(
        [], Message(role="user", content="加功能"), run_id=1,
        mode=MODE_NORMAL, plan_context=VALID_PLAN,
    ))
    assert result.ok
    assert "<approved_plan>" in adapter.systems[0] and "## 验收标准" in adapter.systems[0]
    assert "已采纳的计划" in adapter.systems[0]


def test_plan_whitelist_includes_skill_read():
    """2026-09-24 回归:skill_read 是纯读工具必须进 PLAN 白名单——真机曾暴露
    <skills> 注入后模型调 skill_read 被只读校验拦截(注入了却读不了)。"""
    from otter.loop import PLAN_TOOLS

    assert "skill_read" in PLAN_TOOLS
