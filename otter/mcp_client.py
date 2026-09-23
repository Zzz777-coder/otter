"""MCP 客户端(M4,说明书 P2:stdio 传输,扩展工具生态)。

设计:每个 server 一个隔离会话(单 server 失败不影响其他——vesta 取向);
工具以 deferred 形态注册(经 tool_search 激活,不占常驻 schema);
配置:`~/.otter/mcp.json`,如 {"servers": {"weather": {"command": "uvx", "args": ["mcp-weather"]}}}
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from otter.tools.base import Tool

CONFIG_PATH = Path.home() / ".otter" / "mcp.json"


def load_mcp_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("servers", {})
    except json.JSONDecodeError:
        return {}  # 配置损坏不阻断启动,只跳过 MCP(vesta 取向)


def _wire_schema_to_ours(schema: dict | None) -> dict:
    """MCP inputSchema → otter ToolDefinition.parameters(同为 JSON Schema,直接透传)。"""
    return schema or {"type": "object", "properties": {}}


class McpTool(Tool):
    """一个 MCP server 工具 → 一个 otter 工具(deferred)。"""

    def __init__(self, server_name: str, tool_name: str, description: str, schema: dict, session) -> None:
        self.name = tool_name
        self.description = f"[mcp:{server_name}] {description}"
        self.parameters = _wire_schema_to_ours(schema)
        self.deferred = True  # MCP 工具默认延迟加载
        self._session = session
        self._tool_name = tool_name

    async def run(self, args: dict[str, Any]) -> str:
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._tool_name, arguments=args), timeout=60.0
            )
            texts = [c.text for c in (result.content or []) if hasattr(c, "text")]
            body = "\n".join(texts) or "(空响应)"
            if result.isError:
                return f"[mcp 错误] {body[:2000]}"
            return body[:8000]
        except asyncio.TimeoutError:
            return "[mcp 错误] 工具执行超过 60s"
        except Exception as exc:
            return f"[mcp 错误] {type(exc).__name__}: {exc}"


async def connect_servers(registry) -> list[str]:
    """按配置逐个连 server,列工具并注册(deferred);单点失败只跳过。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    connected: list[str] = []
    for name, cfg in load_mcp_config().items():
        try:
            params = StdioServerParameters(
                command=cfg.get("command", ""), args=list(cfg.get("args", [])),
                env={k: str(v) for k, v in (cfg.get("env") or {}).items()} or None,
            )
            read, write = await stdio_client(params).__aenter__()
            session = await ClientSession(read, write).__aenter__()
            await asyncio.wait_for(session.initialize(), timeout=15.0)
            listing = await session.list_tools()
            for t in listing.tools:
                registry.register(McpTool(name, t.name, t.description or "", t.inputSchema, session))
            connected.append(f"{name}({len(listing.tools)} 工具)")
        except Exception as exc:
            connected.append(f"{name}(失败:{type(exc).__name__},已跳过)")
    return connected
