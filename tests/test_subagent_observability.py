"""子代理可观测性测试(2026-10-25):边界事件 + explore 事件通道 + 历史归属。

三层验证:
- explore:SUBAGENT_STARTED/COMPLETED 边界事件(含结论摘要回带)+ 内部关键
  动作实时上报(via+run_id)+ 节拍事件过滤 + 父 Run 挂接(此前 explore 全黑盒);
- dispatch:同款边界事件,STARTED 带权限级别;
- 历史页:_runs_rows 子代理行带归属前缀;_run_detail 列出"派出子代理"清单。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from otter.models.types import ModelResponse, ModelUsage, ToolCall


class MemStore:
    """最小 Store 面;append_event 记录调用参数(父事件流落库断言用)。"""

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

    async def append_event(self, run_id, type_, payload):
        self.calls.append(("append_event", run_id, type_, payload))


class ExploreAdapter:
    """按脚本回放的假模型:第一次调 read_file,第二次回探索结论。"""

    model = "fake"

    def __init__(self):
        self.calls = 0

    async def complete_stream(self, messages, tools=None, on_text_delta=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[ToolCall(id="c1", name="read_file",
                                     arguments={"path": "README.md"})],
                usage=ModelUsage(10, 5))
        return ModelResponse(content="共找到 3 处相关实现,分别在 a/b/c。", usage=ModelUsage(10, 5))


class _Reg:
    """带最小工具面的假 registry(explore 只读白名单过滤用)。"""

    task_ctx = {"run_id": 77}

    def definitions(self, include_deferred=False):
        class D:
            def __init__(self, name):
                self.name = name
        return [D("read_file")]

    def get(self, name):
        from otter.tools.builtin import ReadFileTool

        return ReadFileTool() if name == "read_file" else None


def test_explore_boundary_events_and_sink(tmp_path: Path, monkeypatch):
    """explore:边界事件起止成对、关键动作带 via+run_id 上报、节拍事件过滤、挂父。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    seen: list[tuple[str, dict]] = []

    async def sink(type_, payload):
        seen.append((type_, payload))

    from otter.subagent import SubagentTool

    store = MemStore()
    tool = SubagentTool(ExploreAdapter(), _Reg(), store)
    tool.set_gates(event_sink=sink)  # 2026-10-25 新增装配口(GUI/CLI 注入)
    r = asyncio.run(tool.run({"task": "找到所有处理审批的函数"}))

    types = [t for t, _ in seen]
    # 边界事件:第一个/最后一个,带任务与结论摘要(用户在界面直接看到派出与答案)
    assert types[0] == "SUBAGENT_STARTED" and types[-1] == "SUBAGENT_COMPLETED"
    started = seen[0][1]
    assert started["via"] == "explore" and started["run_id"] == 11
    assert "审批" in started["task"]
    completed = seen[-1][1]
    assert completed["ok"] is True and completed["steps"] >= 1
    assert "3 处相关实现" in completed["result"] and "duration" in completed
    # 内部关键动作:TOOL_STARTED 带 via+run_id;节拍事件仍过滤(防刷屏口径不变)
    assert "TOOL_STARTED" in types and "MODEL_STARTED" not in types
    tool_ev = next(p for t, p in seen if t == "TOOL_STARTED")
    assert tool_ev["via"] == "explore" and tool_ev["run_id"] == 11
    # 父 Run 挂接(2026-10-25 起 explore 也挂父)+ 边界事件落父事件流(历史可见)
    assert ("new_run", {"parent_run_id": 77}) in store.calls
    parent_events = [c for c in store.calls
                     if c[0] == "append_event" and c[1] == 77
                     and c[2] in ("SUBAGENT_STARTED", "SUBAGENT_COMPLETED")]
    assert len(parent_events) == 2
    assert "3 处相关实现" in r  # 结论原文仍完整回传主循环(header + 4000 字口径不变)


def test_dispatch_boundary_events(tmp_path: Path, monkeypatch):
    """dispatch:边界事件带权限级别;STARTED 在 TOOL_STARTED 之前(派出即上报)。"""
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[str, dict]] = []

    async def sink(type_, payload):
        seen.append((type_, payload))

    from otter.subagent import WritableSubagentTool
    from otter.tools.base import ToolRegistry
    from otter.tools.builtin import BashTool, ReadFileTool, WriteFileTool

    class DispatchAdapter:
        model = "fake"
        calls = 0

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=[ToolCall(id="c1", name="write_file",
                                         arguments={"path": str(tmp_path / "o.txt"),
                                                    "content": "内容"})],
                    usage=ModelUsage(10, 5))
            return ModelResponse(content="已写入完成。", usage=ModelUsage(10, 5))

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(BashTool())
    reg.task_ctx = {"run_id": 88}
    tool = WritableSubagentTool(DispatchAdapter(), reg, MemStore())
    tool.set_gates(event_sink=sink, preview_gate=None)
    asyncio.run(tool.run({"task": "写入 o.txt", "write": "full"}))

    types = [t for t, _ in seen]
    assert types[0] == "SUBAGENT_STARTED"
    started = seen[0][1]
    assert started["via"] == "subagent" and started["write"] == "full"
    assert types[-1] == "SUBAGENT_COMPLETED"
    assert types.index("SUBAGENT_STARTED") < types.index("TOOL_STARTED")


def test_orchestrate_role_boundary_events(tmp_path: Path):
    """orchestrate:planner/worker1/reviewer 三角色各自起止事件成对、via 区分。"""
    seen: list[tuple[str, dict]] = []

    async def sink(type_, payload):
        seen.append((type_, payload))

    class ScriptedAdapter:
        model = "fake"

        def __init__(self, responses):
            self._resp = list(responses)

        async def complete_stream(self, messages, tools=None, on_text_delta=None):
            return self._resp.pop(0)

    ok = ModelUsage(10, 5)
    adapter = ScriptedAdapter([
        ModelResponse(content='{"subtasks": [{"task": "调研", "write": "none"}]}', usage=ok),
        ModelResponse(content="调研完成。", usage=ok),
        ModelResponse(content="通过\n各子任务达标。", usage=ok),
    ])

    from otter.orchestrator import Orchestrator

    orch = Orchestrator(adapter, _Reg(), MemStore(), event_sink=sink)
    out = asyncio.run(orch.run("总任务"))
    assert out["stage"] == "done"
    started = [p["via"] for t, p in seen if t == "SUBAGENT_STARTED"]
    completed = [p["via"] for t, p in seen if t == "SUBAGENT_COMPLETED"]
    assert started == ["planner", "worker1", "reviewer"]
    assert completed == ["planner", "worker1", "reviewer"]
    # worker 内部动作也带 via+run_id 归组标记(worker 用只读面,无工具调用则跳过)


def _seed_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT, stop_reason TEXT,"
               " started_at REAL, finished_at REAL, parent_run_id INTEGER, mode TEXT)")
    db.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, run_id INTEGER, role TEXT,"
               " content TEXT, sequence INTEGER, conversation_id INTEGER)")
    db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, run_id INTEGER, type TEXT,"
               " payload_json TEXT, created_at REAL)")
    db.execute("INSERT INTO runs VALUES (1, 'completed', 'done', 100.0, 200.0, NULL, 'normal')")
    db.execute("INSERT INTO runs VALUES (2, 'completed', 'done', 150.0, 190.0, 1, 'plan')")
    db.execute("INSERT INTO conversations VALUES (5, '调研会话')")
    db.execute("INSERT INTO messages VALUES (1, 1, 'user', '帮我调研X', 0, 5)")
    db.execute("INSERT INTO messages VALUES (2, 2, 'user', '[子任务] 找审批函数', 0, NULL)")
    db.commit()
    db.close()


def test_runs_rows_marks_subagent_line(tmp_path: Path):
    """历史页:子代理 run 单独行带 🦦 前缀与父 Run 归属(2026-10-25 新增)。"""
    from otter.gui import _runs_rows

    db_path = tmp_path / "otter.db"
    _seed_db(db_path)
    rows = _runs_rows(None, db_path=db_path)
    sub = next(r for r in rows if r["conv"] is None)
    assert "🦦" in sub["summary"] and "Run #1" in sub["summary"]


def test_run_detail_lists_spawned_subagents(tmp_path: Path):
    """Trace:父 Run 明细列出名下子 run(派出清单),SUBAGENT 边界事件逐条呈现。"""
    from otter.gui import _run_detail

    db_path = tmp_path / "otter.db"
    _seed_db(db_path)
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO events (run_id, type, payload_json, created_at) VALUES "
               "(1, 'SUBAGENT_STARTED', ?, 160.0)",
               (json.dumps({"via": "explore", "run_id": 2, "task": "找审批函数",
                            "max_steps": 12}),))
    db.commit()
    db.close()
    text = _run_detail(None, 1, db_path=db_path)
    assert "派出子代理" in text and "Run #2" in text
    assert "🦦 派出子代理 explore" in text and "找审批函数" in text
