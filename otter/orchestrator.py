"""L3 角色编排(2026-09-29):planner 分解 → worker fan-out 并行 → reviewer 汇聚。

三层角色模板(同一引擎、独立上下文、不同系统提示):
- planner:把任务分解为自包含子任务清单(严格 JSON,含写权限建议);
- worker:并行执行单个子任务(复用子代理的权限分级与审批通道);
- reviewer:核对全部产物与结论,给出达标判定与缺口清单(不重做,只判与汇总)。

fan-out/join 汇聚:asyncio.gather 并行跑 workers(各 worker 独立 run,
互不占上下文);join 后结论交 reviewer。任何 worker 失败不中断其它
(失败信息如实进 reviewer 输入)。
"""

from __future__ import annotations

import asyncio
import json

from otter.loop import AgentLoop
from otter.models.types import Message
from otter.prompts import SUBAGENT_WRITABLE_SYSTEM
from otter.tools.base import Tool

# ── 角色模板(2026-09-29):planner/worker/reviewer 三件套 ─────────────

PLANNER_SYSTEM = (
    "你是任务规划器。把给定任务分解为 2-4 个可独立执行的子任务,输出严格 JSON"
    "(不要围栏):{\"subtasks\": [{\"task\": \"自包含的子任务描述\", "
    "\"write\": \"none|whitelist|full\", \"paths\": [\"whitelist 时的路径\"]}]}。"
    "每个子任务必须自包含(worker 看不到彼此);子任务合并即完成总任务,"
    "不留缝隙、不重复。读操作用 none,受控文件产出用 whitelist,要跑命令用 full。"
)

REVIEWER_SYSTEM = (
    "你是结果评审员。你会收到总任务与每个子任务的执行结论,逐条核对:"
    "子任务结论是否兑现其分工、合并起来是否完成总任务。输出简明汇总:"
    "第一行「通过」或「未通过」;随后逐子任务一行判定(达标/缺口);"
    "未通过时列出缺口清单(哪个子任务没做什么)。只评不做,不要自己动手补。"
)

_WORKER_SYSTEM = SUBAGENT_WRITABLE_SYSTEM  # worker 与可写子代理同纪律(结论精炼、写入走审批)


def _parse_planner_json(text: str) -> list[dict]:
    """解析 planner 输出;容错剥围栏,坏输出抛 ValueError(上层转失败报告)。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] or ""
        cleaned = cleaned.removeprefix("json").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("planner 未输出 JSON 对象")
    data = json.loads(cleaned[start:end + 1])
    subs = data.get("subtasks")
    if not isinstance(subs, list) or not subs:
        raise ValueError("subtasks 为空或缺失")
    out = []
    for s in subs[:4]:  # 上限 4:fan-out 成本护栏
        task = str(s.get("task", "")).strip()
        if task:
            out.append({"task": task, "write": str(s.get("write", "none")),
                        "paths": [str(p) for p in (s.get("paths") or [])]})
    if not out:
        raise ValueError("subtasks 无有效条目")
    return out


class Orchestrator:
    """编排引擎:一次 run() = planner → workers(gather)→ reviewer。"""

    def __init__(self, adapter, registry, store, preview_gate=None, approval_gate=None,
                 max_steps: int = 12) -> None:
        self._adapter = adapter
        self._registry = registry
        self._store = store
        self._preview_gate = preview_gate
        self._approval_gate = approval_gate
        self._max_steps = max_steps

    def _worker_registry(self, write: str, paths: list[str]):
        """worker 工具面复用子代理权限机制(三级 + 路径围栏)。"""
        from otter.loop import PLAN_TOOLS
        from otter.subagent import _PathGuardedTool
        from otter.tools.base import ToolRegistry

        sub = ToolRegistry()
        for d in self._registry.definitions(include_deferred=True):
            tool = self._registry.get(d.name)
            if tool is None:
                continue
            name = d.name
            if write == "full" or name in PLAN_TOOLS or name == "tool_search":
                sub.register(tool)
            elif write == "whitelist" and name in ("write_file", "edit_file"):
                sub.register(_PathGuardedTool(tool, paths))
        return sub

    async def _role_run(self, system: str, prompt: str, registry, mode: str,
                        max_steps: int) -> tuple[str, bool]:
        """跑一个角色(独立 AgentLoop/独立上下文),返回 (最终文本, 是否正常收尾)。"""
        run_id = await self._store.new_run()
        loop = AgentLoop(self._adapter, registry, self._store,
                         base_system=system,
                         preview_gate=self._preview_gate,
                         approval_gate=self._approval_gate)
        result = await asyncio.wait_for(
            loop.run([], Message(role="user", content=prompt), run_id=run_id,
                     max_steps=max_steps, mode=mode, on_text_delta=None),
            timeout=420.0)
        await self._store.finish_run(run_id, "completed" if result.ok else "failed",
                                     f"orchestra:{result.stop_reason}")
        # ok=False:loop 把模型/工具异常消化为 failed 终态(步数耗尽等同理),
        # 需向上透传给 worker 汇总(执行级成败),语义成败仍由 reviewer 裁定
        return (result.final_text or "").strip(), bool(result and result.ok)

    async def run(self, task: str) -> dict:
        """完整编排:planner 分解 → worker 并行 → reviewer 汇聚。"""
        # 1) planner:纯推理角色,不给工具——带工具时模型会先探索再把 JSON
        # "叙述化"成 markdown 计划,解析反而失败(真机冒烟暴露);分解不需要查现场,
        # 子任务本就要求自包含。reviewer 同理(只评不做)。
        from otter.loop import MODE_NORMAL, MODE_PLAN
        from otter.tools.base import ToolRegistry

        plain_reg = ToolRegistry()  # 空工具面:纯文本一问一答
        plan_text, _ = await self._role_run(PLANNER_SYSTEM, f"[总任务] {task}",
                                            plain_reg, MODE_NORMAL, max_steps=4)
        try:
            subtasks = _parse_planner_json(plan_text)
        except ValueError as exc:
            return {"ok": False, "stage": "plan", "error": str(exc), "plan": plan_text,
                    "workers": [], "review": ""}

        # 2) workers:fan-out 并行(join=gather,失败不互相牵连)
        async def run_worker(i: int, sub: dict) -> dict:
            try:
                reg = self._worker_registry(sub["write"], sub["paths"])
                text, ok = await self._role_run(
                    _WORKER_SYSTEM,
                    f"[子任务 {i + 1}/{len(subtasks)} · 权限={sub['write']}] {sub['task']}",
                    reg, MODE_NORMAL, self._max_steps)
                return {"index": i, "task": sub["task"], "write": sub["write"],
                        "ok": ok, "result": text}
            except Exception as exc:  # 单 worker 失败:如实记录,不中断其它
                return {"index": i, "task": sub["task"], "write": sub["write"],
                        "ok": False, "result": f"执行失败:{type(exc).__name__}: {exc}"}

        workers = await asyncio.gather(*(run_worker(i, s) for i, s in enumerate(subtasks)))
        workers = sorted(workers, key=lambda w: w["index"])

        # 3) reviewer:join 后核对汇总(只读面,只评不做)
        review_input = [f"[总任务] {task}"] + [
            f"[子任务 {w['index'] + 1}] {w['task']}\n[结论] {w['result'][:1500]}"
            for w in workers]
        review, _ = await self._role_run(REVIEWER_SYSTEM, "\n\n".join(review_input),
                                         plain_reg, MODE_NORMAL, max_steps=4)
        return {"ok": review.startswith("通过") or "\n通过" in review[:20],
                "stage": "done", "plan": plan_text, "workers": workers,
                "review": review}


class OrchestrateTool(Tool):
    """orchestrate 工具(2026-09-29 L3):一个调用=完整多角色编排。"""

    name = "orchestrate"
    description = (
        "多角色编排执行复杂任务:规划器分解 → 多个工作代理并行执行 → "
        "评审员核对汇总。适合可拆解的成块工作(多文件改造/调研+起草+核对);"
        "简单任务直接自己做更快,不要滥用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "总任务描述(自包含:目标与验收口径)"},
        },
        "required": ["task"],
    }

    def __init__(self, adapter, registry, store) -> None:
        self._adapter = adapter
        self._registry = registry
        self._store = store
        self._preview_gate = None
        self._approval_gate = None

    def set_gates(self, preview_gate=None, approval_gate=None) -> None:
        """与 dispatch 同款注入:worker 写入走主审批通道。"""
        self._preview_gate = preview_gate
        self._approval_gate = approval_gate

    async def run(self, args) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "[otter] 错误:task 为空"
        orch = Orchestrator(self._adapter, self._registry, self._store,
                            preview_gate=self._preview_gate,
                            approval_gate=self._approval_gate)
        try:
            out = await orch.run(task)
        except Exception as exc:
            return f"[otter] 编排执行失败:{type(exc).__name__}: {exc}"
        if out["stage"] != "done":
            return f"[编排失败 · {out['stage']}] {out.get('error', '')}\n规划器输出:{out.get('plan', '')[:800]}"
        ok_workers = sum(1 for w in out["workers"] if w["ok"])
        header = (f"[编排结论 · {len(out['workers'])} 个子任务({ok_workers} 成功)"
                  f" · 评审:{'通过' if out['ok'] else '未通过'}]\n")
        return header + out["review"][:3000]
