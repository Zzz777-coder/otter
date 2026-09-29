"""子代理(M4 只读探索 + L2 可写执行,2026-09-29 权限分级)。

设计核心(子代理的双价值):子代理独立上下文窗口,只把**结论**
带回主循环——过程细节(读过哪些文件的全文)不污染主上下文。

两级形态:
- explore(M4):只读探索(PLAN 白名单工具),调查类子任务;
- dispatch(L2):可写执行,权限三级——none=只读(同 explore)、
  whitelist=白名单写(write_file/edit_file 限指定路径,围栏外直接拒绝)、
  full=完全写(全工具面,含 bash)。写入统一走主循环的 preview_gate
  审批通道(GUI=diff 卡片批/拒;拒绝不落地),与主代理同一套人工把关。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otter.loop import MODE_NORMAL, MODE_PLAN, AgentLoop, SummaryState
from otter.models.types import Message, ModelUsage
from otter.prompts import SUBAGENT_SYSTEM, SUBAGENT_WRITABLE_SYSTEM  # 2026-09-23 套件化:文案单一来源
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


class _PathGuardedTool(Tool):
    """whitelist 写模式的路径围栏(2026-09-29 L2):包一层写工具,目标路径
    不在允许清单内=直接拒绝执行(返回拒绝说明,模型可换方案)。"""

    def __init__(self, inner: Tool, allowed_roots: list[str]) -> None:
        self._inner = inner
        self._roots = [Path(r).expanduser().resolve() for r in allowed_roots]
        # 转发内层工具的注册面(schema/说明不变,行为加围栏)
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        self.deferred = getattr(inner, "deferred", False)

    def _allowed(self, raw: str) -> bool:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            rp = p.resolve()
        except OSError:
            return False
        return any(rp == r or r in rp.parents for r in self._roots)

    async def run(self, args) -> str:
        path = str(args.get("path", ""))
        if not self._allowed(path):
            return (f"[otter] 白名单写模式:路径 {path} 不在允许清单内,本次调用已拒绝。"
                    "请只操作任务声明的路径,或在结论中说明需要哪个路径。")
        return await self._inner.run(args)


class _ReadOnlyBashTool(Tool):
    """whitelist 档的 bash 硬门(2026-09-29):命令无法静态路径围栏(shell
    转义/子命令可绕过字符串检查),故取「写盘特征即拒」的强约束——
    命中写盘操作特征直接拒绝,读写盘请走 write_file/edit_file(有围栏)
    或把权限升到 full(写盘走人工审批)。读/查询类命令放行。"""

    def __init__(self, inner: Tool) -> None:
        self._inner = inner
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        self.deferred = getattr(inner, "deferred", False)

    async def run(self, args) -> str:
        from otter.loop import _bash_writes_files

        command = str(args.get("command", ""))
        if _bash_writes_files(command):
            return ("[otter] 白名单写模式:bash 不允许执行文件修改操作(命令无法静态路径围栏,"
                    "写盘一律拒绝)。请改用 write_file/edit_file(受路径清单约束),"
                    "或把本次派发升级为 write=full(写盘走人工审批)。")
        return await self._inner.run(args)


def _parent_run_id(registry) -> int | None:
    """从装配层共享的运行上下文取当前父 Run id(主 loop 每次 run 写入;
    拿不到(离线测试/无上下文)返回 None=不挂归属)。"""
    ctx = getattr(registry, "task_ctx", None)
    if isinstance(ctx, dict):
        rid = ctx.get("run_id")
        if isinstance(rid, int):
            return rid
    return None


class WritableSubagentTool(Tool):
    """dispatch 可写子代理(L2):权限三级 none/whitelist/full,写盘复用主审批通道。"""

    name = "dispatch"
    description = (
        "派一个可写子代理独立执行子任务(上下文隔离,只回传结论)。"
        "write=none 只读调查;whitelist 白名单写(只允许改 paths 内的文件,"
        "适合批量生成/受控修改);full 完全写(全工具,含跑命令,适合独立交付的"
        "成块工作)。写入会走人工审批,拒绝的写入不落地。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "子任务描述(自包含:目标、文件、验收口径)"},
            "write": {"type": "string", "enum": ["none", "whitelist", "full"],
                      "description": "权限级别(默认 none)"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "whitelist 模式允许写入的路径清单(文件或目录,必填)"},
        },
        "required": ["task"],
    }

    def __init__(self, adapter, registry, store) -> None:
        self._adapter = adapter
        self._registry = registry
        self._store = store
        self._preview_gate = None   # GUI 注入(diff 卡片批/拒);CLI 保持 None=自动通过
        self._approval_gate = None  # 同上(bash 等非写入类审批)
        self._event_sink = None     # 2026-09-29 子代理过程实时上报(主界面同一事件通道)

    def set_gates(self, preview_gate=None, approval_gate=None, event_sink=None) -> None:
        """装配期注入审批通道与事件上报(与 artifact_publish.preview_fn 同模式;GUI 每任务装配时调)。"""
        self._preview_gate = preview_gate
        self._approval_gate = approval_gate
        self._event_sink = event_sink

    def _build_registry(self, level: str, paths: list[str]):
        """按权限级别构造子代理工具面(围栏在工具层,模型绕不过)。"""
        from otter.loop import PLAN_TOOLS
        from otter.tools.base import ToolRegistry

        sub = ToolRegistry()
        if level == "full":
            for d in self._registry.definitions(include_deferred=True):
                tool = self._registry.get(d.name)
                if tool is not None:
                    sub.register(tool)
            return sub
        if level == "whitelist":
            for d in self._registry.definitions(include_deferred=True):
                name = d.name
                tool = self._registry.get(name)
                if tool is None:
                    continue
                if name in PLAN_TOOLS or name == "tool_search":
                    sub.register(tool)
                elif name in ("write_file", "edit_file"):
                    # 2026-09-29 L2:写工具包路径围栏——清单外路径在执行前拒绝
                    sub.register(_PathGuardedTool(tool, paths))
                elif name == "bash":
                    # 2026-09-29 补:whitelist 档给 bash 但加写盘硬门——
                    # "生成后跑测试自检"类任务不必升 full;写盘仍只能走围栏内的写工具
                    sub.register(_ReadOnlyBashTool(tool))
            return sub
        # none:与 explore 同面(PLAN 白名单 + tool_search)
        for d in self._registry.definitions(include_deferred=True):
            if d.name in PLAN_TOOLS or d.name == "tool_search":
                tool = self._registry.get(d.name)
                if tool is not None:
                    sub.register(tool)
        return sub

    async def run(self, args) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "[otter] 错误:task 为空"
        level = str(args.get("write", "none"))
        if level not in ("none", "whitelist", "full"):
            return f"[otter] 错误:write 只能是 none/whitelist/full(收到 {level})"
        paths = [str(p) for p in (args.get("paths") or []) if str(p).strip()]
        if level == "whitelist" and not paths:
            return "[otter] 错误:whitelist 模式必须给 paths(允许写入的文件或目录清单)"

        sub_registry = self._build_registry(level, paths)
        events: list[str] = []
        sink = self._event_sink

        async def on_event(type_: str, payload: dict) -> None:
            events.append(type_)
            # 2026-09-29 过程可视化:子代理事件实时上报主界面(与主代理同一
            # 事件通道,via 标记来源;上报失败静默——可视化不阻断子代理本身)。
            # 节拍事件(MODEL_STARTED/COMPLETED)不上报——子代理步数多,
            # 逐拍转发会在主界面刷大量分隔行,只报关键动作
            if sink is not None and type_ not in ("MODEL_STARTED", "MODEL_COMPLETED"):
                try:
                    await sink(type_, {**payload, "via": "subagent"})
                except Exception:
                    pass

        # 2026-09-29 父 Run 挂接:历史页可见子代理 run 归属(拿不到上下文=不挂)
        run_id = await self._store.new_run(parent_run_id=_parent_run_id(self._registry))
        loop = AgentLoop(
            self._adapter, sub_registry, self._store,
            on_event=on_event,
            base_system=SUBAGENT_WRITABLE_SYSTEM,
            # 2026-09-29 L2:写盘审批复用主循环通道——GUI 场景弹同款 diff 卡片,
            # 拒绝(fail-closed)由 loop 拦截,文件不落地
            preview_gate=self._preview_gate,
            approval_gate=self._approval_gate,
        )
        result = await asyncio.wait_for(
            loop.run(
                [], Message(role="user", content=f"[子任务 · 权限={level}] {task}"),
                run_id=run_id, max_steps=15,
                mode=MODE_PLAN if level == "none" else MODE_NORMAL,
                on_text_delta=None,
            ),
            timeout=420.0,
        )
        await self._store.finish_run(run_id, "completed" if result.ok else "failed",
                                     f"subagent:dispatch:{result.stop_reason}")
        writes = len([e for e in events if e == "DIFF_PREVIEW"])
        header = (f"[子代理结论 · 权限={level} · {result.steps} 步 · "
                  f"{len([e for e in events if e == 'TOOL_COMPLETED'])} 次工具调用 · "
                  f"{writes} 次写入审批]\n")
        return header + (result.final_text or "(无结论)")[:4000]
