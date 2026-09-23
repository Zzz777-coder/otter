"""deferred 工具 + tool_search 激活(M3,说明书 5.1/2.1 借鉴 vesta 的省 token 机制)。

机制:低频工具(memory_*/evidence_*)不进每步 schema(工具定义是最大 token 税之一);
模型用 tool_search 按关键词找到后,下一步请求才携带其 schema,执行层对
"未激活就调用"做硬拒绝——省下的上下文对弱模型直接变成可用预算。
"""

from __future__ import annotations

from typing import Any

from otter.tools.base import Tool


class ToolSearchTool(Tool):
    """关键词搜索工具目录并激活命中项;激活下一步生效(vesta 的延迟生效语义)。"""

    name = "tool_search"
    description = (
        "搜索可用工具目录(低频工具默认不列出,如记忆/证据类)。"
        "按关键词搜索并查看用法;命中后下一步即可直接调用。"
    )
    parameters = {
        "type": "object",
        "properties": {"keywords": {"type": "string", "description": "空格分隔关键词,如 'memory 记忆'"}},
        "required": ["keywords"],
    }

    def __init__(self, registry, activated: set[str]) -> None:
        self.registry = registry
        self.activated = activated  # 与 loop 共享的激活集合

    async def run(self, args: dict[str, Any]) -> str:
        keywords = [k.lower() for k in str(args.get("keywords", "")).split() if k]
        hits = []
        for d in self.registry.definitions(include_deferred=True):
            hay = f"{d.name} {d.description}".lower()
            if any(k in hay for k in keywords):
                hits.append(d)
                self.activated.add(d.name)  # 激活:下一步 schema 携带
        if not hits:
            avail = [d.name for d in self.registry.definitions(include_deferred=True)]
            return f"[otter] 无命中。当前目录:{', '.join(avail)}"
        body = "\n".join(f"• {d.name} — {d.description} 参数:{d.parameters}" for d in hits[:5])
        return f"[otter] 已激活以下工具(下一步即可调用):\n{body}"
