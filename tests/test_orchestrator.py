"""L3 角色编排测试(2026-09-29):planner 分解 → worker 并行 → reviewer 汇聚。

离线验证:按 system 提示路由的假模型驱动三角色;断言分解→fan-out→join
全链路、worker 并行执行、单 worker 失败不牵连、评审收到全部结论。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otter.models.types import ModelResponse, ModelUsage
from otter.orchestrator import Orchestrator, _parse_planner_json

PLAN_JSON = (
    '{"subtasks": ['
    '{"task": "调研目录结构", "write": "none"},'
    '{"task": "生成报告文件", "write": "whitelist", "paths": ["out.md"]},'
    '{"task": "核对报告内容", "write": "none"}'
    "]}"
)


class RoleAdapter:
    """按 base_system 路由的假模型:planner 回 JSON,worker/reviewer 回文本。"""

    model = "fake"

    def __init__(self, worker_fail: bool = False):
        self._fail = worker_fail
        self.worker_prompts: list[str] = []
        self.reviewer_prompt: str = ""

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        system = messages[0].content if messages and messages[0].role == "system" else ""
        user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if "任务规划器" in system:
            return ModelResponse(content=PLAN_JSON, usage=ModelUsage(50, 20))
        if "执行子代理" in system:
            self.worker_prompts.append(user)
            if self._fail and len(self.worker_prompts) == 2:
                raise RuntimeError("worker 执行环境崩溃")  # 执行级异常(非"结论说没完成")
            return ModelResponse(content=f"完成:{user[:30]}", usage=ModelUsage(30, 10))
        if "结果评审员" in system:
            self.reviewer_prompt = user
            return ModelResponse(content="通过\n子任务1 达标\n子任务2 达标", usage=ModelUsage(40, 15))
        return ModelResponse(content="(未知角色)", usage=ModelUsage(1, 1))


class MemStore:
    async def new_run(self):
        return 21

    async def finish_run(self, *a):
        pass

    async def append_message(self, *a):
        pass

    async def save_checkpoint(self, *a, **k):
        pass

    async def append_event(self, *a):
        pass


def _make(adapter):
    from otter.orchestrator import Orchestrator
    from otter.tools.base import ToolRegistry
    from otter.tools.builtin import ReadFileTool, WriteFileTool

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    return Orchestrator(adapter, reg, MemStore())


def test_planner_json_parsing():
    subs = _parse_planner_json('```json\n{"subtasks": [{"task": "a", "write": "none"}]}\n```')
    assert subs == [{"task": "a", "write": "none", "paths": []}]
    for bad in ("没有 json", '{"subtasks": []}', '{"other": 1}'):
        try:
            _parse_planner_json(bad)
            assert False, f"应拒绝:{bad}"
        except ValueError:
            pass


def test_orchestrate_full_pipeline(tmp_path: Path, monkeypatch):
    """分解 → 并行执行 → 汇总:3 个 worker 都被调用,reviewer 收到全部结论。"""
    monkeypatch.chdir(tmp_path)
    adapter = RoleAdapter()
    orch = _make(adapter)
    out = asyncio.run(orch.run("做一份目录调研报告"))
    assert out["stage"] == "done" and out["ok"] is True
    assert len(out["workers"]) == 3
    assert all(w["ok"] for w in out["workers"])
    # reviewer 的输入里带总任务与三个子任务结论(join 的证据)
    assert "目录调研报告" in adapter.reviewer_prompt
    assert adapter.reviewer_prompt.count("[结论]") == 3
    # worker 提示带编号与权限标记(fan-out 的证据)
    assert any("子任务 1/3" in p and "权限=none" in p for p in adapter.worker_prompts)
    assert any("权限=whitelist" in p for p in adapter.worker_prompts)


def test_orchestrate_worker_failure_isolated(tmp_path: Path, monkeypatch):
    """单 worker 失败:不中断其它 worker,失败信息如实进 reviewer 输入。"""
    monkeypatch.chdir(tmp_path)
    adapter = RoleAdapter(worker_fail=True)
    orch = _make(adapter)
    out = asyncio.run(orch.run("任务"))
    assert len(out["workers"]) == 3
    ok_n = sum(1 for w in out["workers"] if w["ok"])
    assert ok_n == 2  # 异常的只有第 2 个,其余照常完成(隔离语义)
    # 失败信息原样进入评审视野(语义成败由 reviewer 裁定,worker 的 ok 只管执行级异常)
    assert "执行环境崩溃" in adapter.reviewer_prompt


def test_orchestrate_bad_plan_reports_stage(tmp_path: Path, monkeypatch):
    """planner 输出不可解析:stage=plan 失败报告,不进 worker 阶段。"""

    class BadPlanAdapter(RoleAdapter):
        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            system = messages[0].content if messages and messages[0].role == "system" else ""
            if "任务规划器" in system:
                return ModelResponse(content="我拒绝输出 JSON", usage=ModelUsage(5, 5))
            return await super().complete_stream(messages, tools, on_text_delta)

    monkeypatch.chdir(tmp_path)
    adapter = BadPlanAdapter()
    out = asyncio.run(_make(adapter).run("任务"))
    assert out["stage"] == "plan" and out["ok"] is False
    assert adapter.worker_prompts == []  # 未进入 worker 阶段
