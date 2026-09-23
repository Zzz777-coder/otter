"""子代理(M4,说明书 P2:并行只读探索 + 上下文隔离)。

设计(学自 Claude Code 的 subagent 双价值):子代理独立上下文窗口,只把**结论**
带回主循环——过程细节(读过哪些文件的全文)不污染主上下文。v1 限定只读探索
(read/grep/glob/repo_map),不做写盘子代理(说明书"可以缩"的取舍)。
"""

from __future__ import annotations

import asyncio

from otter.loop import MODE_PLAN, AgentLoop, SummaryState
from otter.models.types import Message, ModelUsage
from otter.prompts import SUBAGENT_SYSTEM  # 2026-09-23 套件化:文案单一来源
from otter.tools.base import Tool


class SubagentTool(Tool):
    """spawn 子代理并行探索:主循环一个工具调用=一个隔离子任务。"""

    name = "explore"
    description = (
        "派一个子代理去独立探索(只读):大范围找代码/读多个文件/回答调研问题,"
        "只返回精炼结论不占主上下文。适合并行拆解的检索型子任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "子任务描述,如'找到所有处理审批的函数并总结各自职责'"},
        },
        "required": ["task"],
    }

    def __init__(self, adapter, registry, store) -> None:
        self._adapter = adapter
        self._registry = registry  # 只读子集在 run 里按 PLAN 白名单过滤
        self._store = store

    async def run(self, args) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "[otter] 错误:task 为空"

        # 子代理专用最小 registry(只读白名单内工具)
        from otter.loop import PLAN_TOOLS
        from otter.tools.base import ToolRegistry

        sub_registry = ToolRegistry()
        for d in self._registry.definitions(include_deferred=True):
            if d.name in PLAN_TOOLS or d.name in ("tool_search",):
                tool = self._registry.get(d.name)
                if tool is not None:
                    sub_registry.register(tool)

        events: list[str] = []

        async def on_event(type_: str, payload: dict) -> None:
            events.append(type_)

        run_id = await self._store.new_run()
        loop = AgentLoop(
            self._adapter, sub_registry, self._store,
            on_event=on_event,
            base_system=SUBAGENT_SYSTEM,  # 2026-09-23 修复 wiring:此前该提示从未注入,子代理跑的是主循环 BASE_SYSTEM
        )  # 子代理限步在 run() 传:探索型任务 12 步足够
        result = await asyncio.wait_for(
            loop.run(
                [], Message(role="user", content=f"[子任务] {task}"),
                run_id=run_id, max_steps=12, mode=MODE_PLAN,
                on_text_delta=None,
            ),
            timeout=300.0,
        )
        await self._store.finish_run(run_id, "completed" if result.ok else "failed",
                                     f"subagent:{result.stop_reason}")
        header = f"[子代理结论 · {result.steps} 步 · {len([e for e in events if e == 'TOOL_COMPLETED'])} 次工具调用]\n"
        return header + (result.final_text or "(无结论)")[:4000]
