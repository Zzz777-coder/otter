"""工具子系统:Tool 抽象与注册表。

设计参考 vesta app/tools/registry.py 的注册表思想(M0 最小版;权限/审批/沙箱
三层 fail-closed 与 deferred 工具按说明书路线图在 M2/M3 引入)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from otter.models.types import ToolDefinition


class Tool(ABC):
    """内置工具基类。子类声明 name/description/parameters 并实现 run。
    deferred=True 的工具不进默认 schema,需先 tool_search 激活(M3)。"""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    deferred: bool = False  # M3:低频工具延迟加载,省上下文

    @abstractmethod
    async def run(self, args: dict[str, Any]) -> str:
        """执行工具,返回文本结果(错误也以文本返回并标注,交由模型自行调整)。"""


class ToolRegistry:
    """工具注册表:聚合 definitions 供请求组装,loop 按名字分发执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def definitions(self, active_extra: set[str] | None = None,
                    include_deferred: bool = False) -> list[ToolDefinition]:
        """默认只含非 deferred 工具;active_extra 为本会话已激活名单(tool_search 命中)。
        顺序稳定(按注册顺序),为提示缓存前缀复用打基础。"""
        out = []
        for t in self._tools.values():
            if t.deferred and not include_deferred and t.name not in (active_extra or set()):
                continue
            out.append(ToolDefinition(name=t.name, description=t.description, parameters=t.parameters))
        return out

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)
