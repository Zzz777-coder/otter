"""L2 可写子代理测试(2026-09-29):权限分级 + 路径围栏 + 审批拒绝不落地。

三层验证:
- 工具面:三级权限各自包含/排除的工具(dispatch._build_registry);
- 路径围栏:whitelist 清单外写入在工具层被拒(文件不落地);
- 审批通道:preview_gate 拒绝 → loop 拦截写入(fail-closed),批准 → 落地。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otter.models.types import ModelResponse, ModelUsage, ToolCall


class MemStore:
    """子代理 run 生命周期所需的最小 Store 面(与 test_m4 同款);
    2026-09-29 记录 new_run 调用参数(父 Run 挂接断言用)。"""

    def __init__(self):
        self.calls: list[tuple] = []

    async def new_run(self, parent_run_id=None):
        self.calls.append(("new_run", {"parent_run_id": parent_run_id}))
        return 11

    async def finish_run(self, *a):
        pass

    async def append_message(self, *a):
        pass

    async def save_checkpoint(self, *a, **k):
        pass

    async def append_event(self, *a):
        pass


class ScriptAdapter:
    """按脚本回放的假模型:第一次发 write_file 调用,第二次回最终结论。"""

    model = "fake"

    def __init__(self, write_path: str, content: str):
        self._path = write_path
        self._content = content
        self.calls = 0

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[ToolCall(id="c1", name="write_file",
                                     arguments={"path": self._path, "content": self._content})],
                usage=ModelUsage(10, 5))
        return ModelResponse(content="子代理已完成写入。", usage=ModelUsage(10, 5))


def _make_tool(tmp_path: Path, adapter, with_task_ctx: dict | None = None):
    from otter.subagent import WritableSubagentTool
    from otter.tools.base import ToolRegistry
    from otter.tools.builtin import BashTool, ReadFileTool, WriteFileTool

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(BashTool())
    if with_task_ctx is not None:  # 模拟主 loop 写入的运行上下文(父 Run 挂接来源)
        reg.task_ctx = dict(with_task_ctx)
    return WritableSubagentTool(adapter, reg, MemStore())


def test_dispatch_argument_validation(tmp_path: Path):
    tool = _make_tool(tmp_path, ScriptAdapter(str(tmp_path / "a.txt"), "x"))
    r = asyncio.run(tool.run({"task": "做点事", "write": "banana"}))
    assert "错误" in r and "none/whitelist/full" in r
    r = asyncio.run(tool.run({"task": "做点事", "write": "whitelist"}))
    assert "paths" in r  # whitelist 必须给路径清单
    r = asyncio.run(tool.run({"task": ""}))
    assert "task 为空" in r


def test_dispatch_registry_levels(tmp_path: Path):
    """三级工具面:none 不含写与 bash;whitelist 含围栏版写工具+硬门 bash;full 全量。"""
    tool = _make_tool(tmp_path, ScriptAdapter(str(tmp_path / "a.txt"), "x"))

    names_none = {d.name for d in tool._build_registry("none", []).definitions(include_deferred=True)}
    assert "write_file" not in names_none and "bash" not in names_none

    names_wl = {d.name for d in tool._build_registry("whitelist", [str(tmp_path)]).definitions(include_deferred=True)}
    assert "write_file" in names_wl and "bash" in names_wl  # 2026-09-29:bash 进 whitelist(硬门)

    names_full = {d.name for d in tool._build_registry("full", []).definitions(include_deferred=True)}
    assert "write_file" in names_full and "bash" in names_full


def test_dispatch_whitelist_fence_rejects_outside(tmp_path: Path, monkeypatch):
    """路径围栏:清单外写入被拒(不落地),清单内放行。"""
    monkeypatch.chdir(tmp_path)
    inside = tmp_path / "allowed"
    inside.mkdir()
    adapter = ScriptAdapter(str(tmp_path / "outside.txt"), "不该写这里")
    tool = _make_tool(tmp_path, adapter)
    r = asyncio.run(tool.run({
        "task": "写入 outside.txt", "write": "whitelist", "paths": [str(inside)]}))
    assert "子代理结论" in r
    assert not (tmp_path / "outside.txt").exists()  # 围栏拒绝:清单外不落地

    adapter2 = ScriptAdapter(str(inside / "ok.txt"), "正常内容")
    tool2 = _make_tool(tmp_path, adapter2)
    asyncio.run(tool2.run({
        "task": "写入 ok.txt", "write": "whitelist", "paths": [str(inside)]}))
    assert (inside / "ok.txt").read_text(encoding="utf-8") == "正常内容"  # 清单内落地


def test_dispatch_preview_gate_deny_and_allow(tmp_path: Path, monkeypatch):
    """审批通道:gate=False → 写入被拦不落地(结论标注拒绝);gate=True → 落地。"""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "gate.txt"

    adapter = ScriptAdapter(str(target), "内容")
    tool = _make_tool(tmp_path, adapter)

    async def deny(_diff):
        return False

    tool.set_gates(preview_gate=deny)
    r = asyncio.run(tool.run({"task": "写入 gate.txt", "write": "full"}))
    assert "子代理结论" in r and "权限=full" in r
    assert not target.exists()  # 拒绝路径:diff 已给人看,但不落盘

    async def allow(_diff):
        return True

    adapter2 = ScriptAdapter(str(target), "批准后的内容")
    tool2 = _make_tool(tmp_path, adapter2)
    tool2.set_gates(preview_gate=allow)
    r2 = asyncio.run(tool2.run({"task": "写入 gate.txt", "write": "full"}))
    assert "1 次写入审批" in r2  # header 统计 DIFF_PREVIEW 一次
    assert target.read_text(encoding="utf-8") == "批准后的内容"  # 批准路径:落地


# ── 2026-09-29 四项增强:bash 硬门 / 依赖调度 / 事件上报 / 父 Run 挂接 ──────

def test_whitelist_bash_write_gate(tmp_path: Path, monkeypatch):
    """whitelist 档 bash:写盘命令硬拒(命令无法静态围栏),只读命令放行。"""
    monkeypatch.chdir(tmp_path)
    from otter.subagent import _ReadOnlyBashTool
    from otter.tools.builtin import BashTool

    guarded = _ReadOnlyBashTool(BashTool())
    r = asyncio.run(guarded.run({"command": "echo hi > out.txt"}))
    assert "白名单写模式" in r and "write_file" in r  # 拒绝并指路
    assert not (tmp_path / "out.txt").exists()         # 未执行
    r2 = asyncio.run(guarded.run({"command": "ls -la"}))
    assert "白名单" not in r2                          # 只读放行(正常执行)


def test_dispatch_registry_whitelist_has_gated_bash(tmp_path: Path):
    """whitelist 工具面:bash 在场(带硬门),不再是 full 独占。"""
    tool = _make_tool(tmp_path, ScriptAdapter(str(tmp_path / "a.txt"), "x"))
    names_wl = {d.name for d in tool._build_registry("whitelist", [str(tmp_path)]).definitions(include_deferred=True)}
    assert "bash" in names_wl and "write_file" in names_wl
    names_none = {d.name for d in tool._build_registry("none", []).definitions(include_deferred=True)}
    assert "bash" not in names_none  # none 档仍只读


def test_dispatch_event_sink_and_parent_run(tmp_path: Path, monkeypatch):
    """过程上报:子代理事件带 via=子代理 进 sink;run 挂到父 Run(经 registry.task_ctx)。"""
    monkeypatch.chdir(tmp_path)
    seen_events: list[tuple[str, dict]] = []

    async def sink(type_, payload):
        seen_events.append((type_, payload))

    adapter = ScriptAdapter(str(tmp_path / "sink.txt"), "内容")
    tool = _make_tool(tmp_path, adapter, with_task_ctx={"run_id": 77})
    tool.set_gates(event_sink=sink)
    asyncio.run(tool.run({"task": "写 sink.txt", "write": "full"}))

    types = [t for t, _ in seen_events]
    assert "MODEL_STARTED" in types and "TOOL_STARTED" in types  # 子代理过程上报了
    assert all(p.get("via") == "subagent" for _, p in seen_events)  # 来源标记齐
    # 父 Run 挂接:store 里该 run 的 parent_run_id=77(用假 Store 捕获构造参数)
    calls = [c for c in tool._store.calls if c[0] == "new_run"]
    assert calls and calls[0][1] == {"parent_run_id": 77}


def test_orchestrate_dependency_layers_and_upstream(tmp_path: Path, monkeypatch):
    """depends_on:依赖分层(前置先完成),前序结论注入后置 worker 输入;环=如实报告。"""
    monkeypatch.chdir(tmp_path)
    from otter.orchestrator import Orchestrator

    PLAN = ('{"subtasks": ['
            '{"task": "第一步调研", "write": "none"},'
            '{"task": "第二步引用前者的产出做结论", "write": "none", "depends_on": [1]},'
            '{"task": "独立并行项", "write": "none"}'
            ']}')

    class DepAdapter:
        model = "fake"

        def __init__(self):
            self.second_prompt = ""

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            system = messages[0].content if messages and messages[0].role == "system" else ""
            user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "任务规划器" in system:
                return ModelResponse(content=PLAN, usage=ModelUsage(10, 5))
            if "执行子代理" in system:
                if "第二步" in user:
                    self.second_prompt = user  # 记录后置 worker 收到的完整输入
                return ModelResponse(content=f"完成:{user[:20]}", usage=ModelUsage(10, 5))
            if "结果评审员" in system:
                return ModelResponse(content="通过", usage=ModelUsage(10, 5))
            return ModelResponse(content="?", usage=ModelUsage(1, 1))

    adapter = DepAdapter()
    orch = _make_orch(adapter)
    out = asyncio.run(orch.run("任务"))
    assert out["stage"] == "done" and len(out["workers"]) == 3
    # 前置结论注入:后置 worker 的输入里带第一步的结论(跨层信息流)
    assert "前置子任务结论" in adapter.second_prompt
    assert "[子任务 1 结论]" in adapter.second_prompt  # 前置结论以标记块注入

    # 环:2 依赖 3、3 依赖 2 → 都未执行,如实报告
    CYCLIC = ('{"subtasks": ['
              '{"task": "A", "write": "none"},'
              '{"task": "B", "write": "none", "depends_on": [3]},'
              '{"task": "C", "write": "none", "depends_on": [2]}'
              ']}')

    class CycAdapter(DepAdapter):
        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            system = messages[0].content if messages and messages[0].role == "system" else ""
            if "任务规划器" in system:
                return ModelResponse(content=CYCLIC, usage=ModelUsage(10, 5))
            return await DepAdapter.complete_stream(self, messages, tools, on_text_delta)

    out2 = asyncio.run(_make_orch(CycAdapter()).run("任务"))
    stuck = [w for w in out2["workers"] if "依赖成环" in w["result"]]
    assert len(stuck) == 2  # B/C 未执行且如实标注;A 正常跑


def _make_orch(adapter):
    from otter.orchestrator import Orchestrator
    from otter.tools.base import ToolRegistry
    from otter.tools.builtin import ReadFileTool

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    return Orchestrator(adapter, reg, MemStore())
