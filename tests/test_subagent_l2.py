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
    """子代理 run 生命周期所需的最小 Store 面(与 test_m4 同款)。"""

    async def new_run(self):
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


def _make_tool(tmp_path: Path, adapter):
    from otter.subagent import WritableSubagentTool
    from otter.tools.base import ToolRegistry
    from otter.tools.builtin import BashTool, ReadFileTool, WriteFileTool

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(BashTool())
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
    """三级工具面:none 不含写;whitelist 含围栏版 write_file 不含 bash;full 全量。"""
    tool = _make_tool(tmp_path, ScriptAdapter(str(tmp_path / "a.txt"), "x"))

    names_none = {d.name for d in tool._build_registry("none", []).definitions(include_deferred=True)}
    assert "write_file" not in names_none and "bash" not in names_none

    names_wl = {d.name for d in tool._build_registry("whitelist", [str(tmp_path)]).definitions(include_deferred=True)}
    assert "write_file" in names_wl and "bash" not in names_wl

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
