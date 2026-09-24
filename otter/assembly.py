"""M2+M3 公共装配:工具注册表 / 审批门 / 沙箱 / 记忆包 / repo map。

独立成模块以避免循环导入(evidence/memory 复用 builtin,反向不能引用)。
M3:memory_*/evidence_* 标记 deferred(不进默认 schema,经 tool_search 激活);
repo_map 与 tool_search 常驻(小 schema 高价值)。
"""

from __future__ import annotations

from pathlib import Path

from otter.evidence import EvidenceListTool, EvidenceReadTool
from otter.memory_runtime import build_memory_tools
from otter.repo_map import RepoMap, RepoMapTool
from otter.sandbox import SandboxBackend, SeatbeltSandbox, make_sandbox
from otter.store import Store
from otter.tools.base import Tool, ToolRegistry
from otter.tools.builtin import builtin_registry
from otter.tools.deferred import ToolSearchTool
from otter.tools.permissions import (
    ALLOW,
    ASK,
    DENY,
    ApprovalGate,
    ConsoleApprovalGate,
    PermissionEngine,
    Rule,
    SessionMemory,
    WebApprovalGate,
)


def _mark_deferred(tool: Tool) -> Tool:
    tool.deferred = True  # M3:低频工具延迟加载,省每步 schema 开销
    return tool


def full_registry(store: Store, activated: set[str] | None = None,
                  with_memory: bool = True, with_repo_map: bool = True) -> ToolRegistry:
    """内置 + Evidence + 记忆 + repo_map + tool_search;返回 (registry, memory_bundle)由 build_full 提供。"""
    registry = builtin_registry()
    registry.register(_mark_deferred(EvidenceReadTool(store)))
    registry.register(_mark_deferred(EvidenceListTool(store)))
    if with_repo_map:
        registry.register(RepoMapTool(RepoMap()))
    # M4:Artifact 产物发布 + Skill 列表 + Replan 领域包(deferred)
    from otter.artifact import ArtifactPublishTool
    from otter.skills import SkillListTool, SkillReadTool

    # 2026-09-23 artifact_publish 改为常驻(用户要求文件预览,模型需随时可见)
    registry.register(ArtifactPublishTool())
    registry.register(_mark_deferred(SkillListTool()))
    # 2026-09-24 Skill 运行时注入配套:brief 常驻 system,正文按需读(deferred 省 schema)
    registry.register(_mark_deferred(SkillReadTool()))
    # Replan 领域工具包(说明书 M4 示范):get_schedule/simulate/commit
    import os

    if os.environ.get("OTTER_REPLAN", "1") not in ("0", "false", "no"):
        from otter.replan import build_replan_tools

        for t in build_replan_tools():
            registry.register(t)  # 常驻(领域主工具面,共 3 个 schema 可承受)
    activated = activated if activated is not None else set()
    registry.register(ToolSearchTool(registry, activated))
    return registry


def build_full(store: Store, memory_root: Path | None = None,
               activated: set[str] | None = None, with_memory: bool = True,
               adapter=None) -> tuple[ToolRegistry, tuple | None]:
    """完整装配:registry + memory_bundle(None 当 with_memory=False);adapter 供子代理复用(M4)。"""
    registry = full_registry(store, activated=activated, with_memory=with_memory)
    bundle = None
    if with_memory:
        tools, mem_store, core, mem_ctx = build_memory_tools(memory_root)
        for t in tools:
            registry.register(_mark_deferred(t))
        bundle = (mem_store, core, mem_ctx)
    # M4 子代理:explore 工具(只读探索,上下文隔离);需要 adapter
    if adapter is not None:
        from otter.subagent import SubagentTool

        registry.register(SubagentTool(adapter, registry, store))
    return registry, bundle


def build_gate(window=None) -> ApprovalGate:
    """审批门:GUI 传 window(confirm 弹窗),CLI 不传(终端三选项)。"""
    engine = PermissionEngine()
    memory = SessionMemory()
    if window is not None:
        return WebApprovalGate(engine, memory, window)
    return ConsoleApprovalGate(engine, memory)


__all__ = [
    "full_registry", "build_full", "build_gate", "make_sandbox", "SeatbeltSandbox",
    "SandboxBackend", "PermissionEngine", "SessionMemory", "Rule", "ALLOW", "ASK", "DENY",
]
