"""MCP 客户端完整版(v0.5,2026-09-24)——域模型/状态机/失败回滚/mcp_status。

底层传输沿用官方 mcp SDK(M4 起已用):
- MCPServerConfig/MCPSettings:pydantic 域模型(name 约束/重名拒绝/超时/enabled);
- MCPConfigurationStore:配置统一校验+原子写,add/set_enabled/delete 供
  /mcp 命令与 Extensions 导入共用;
- MCPClientManager:状态机 stopped→starting→running/failed;启动失败回滚
  本 server 已注册的工具(不留半连接半注册);单 server 失败不拖累其他(隔离取向);
- mcp__<server>__<tool> 命名空间(防跨 server 工具名冲突);
- mcp_status 常驻只读工具:不启动 server 即可看配置与状态快照。

与 的差异(刻意):otter 无 per-server SandboxConfig/ToolPermission 域
(权限统一走 permissions.py 默认表,MCP 工具未命中默认表=ASK 弹审批,fail-closed);
env 展开沿用官方 SDK 的默认环境继承。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from otter.tools.base import Tool

CONFIG_PATH = Path.home() / ".otter" / "mcp.json"
_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_INVALID_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_]+")


# ── 域模型( ───────────────────────────────

class MCPServerState:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"


class MCPServerConfig(BaseModel):
    """一个 stdio MCP Server 的静态配置(名称/超时等域校验)。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    startup_timeout_seconds: float = Field(default=15.0, gt=0)
    call_timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SERVER_NAME_RE.fullmatch(value):
            raise ValueError("name 只能包含字母、数字和下划线")
        return value


class MCPSettings(BaseModel):
    """配置文件顶层结构(servers 列表形态;兼容旧 dict 形态读取)。"""

    model_config = ConfigDict(extra="forbid")
    servers: tuple[MCPServerConfig, ...] = ()

    @field_validator("servers")
    @classmethod
    def reject_duplicate_names(cls, value):
        names = [s.name for s in value]
        if len(names) != len(set(names)):
            raise ValueError("MCP Server 名称不能重复")
        return value


class MCPServerStatus(BaseModel):
    """状态快照(mcp_status 工具与 /mcp 命令共用)。"""

    model_config = ConfigDict(extra="forbid")
    name: str
    state: str
    tool_names: tuple[str, ...] = ()
    error: str | None = None


def mcp_tool_name(server_name: str, remote_name: str) -> str:
    """模型可见命名空间:mcp__<server>__<tool>,非法字符折为 _。"""
    normalized = _INVALID_TOOL_NAME.sub("_", remote_name).strip("_")
    if not normalized:
        raise ValueError(f"MCP Server '{server_name}' 返回了无法注册的工具名 {remote_name!r}")
    return f"mcp__{server_name}__{normalized}"


# ── 配置存储( 的 MCPConfigurationStore) ────

def _normalize_legacy(raw: dict) -> dict:
    """旧 dict 形态 {"servers": {"name": {...}}} → 列表形态(向后兼容 M4 配置)。"""
    servers = raw.get("servers")
    if isinstance(servers, dict):
        raw = {**raw, "servers": [{"name": k, **v} for k, v in servers.items()]}
    return raw


class MCPConfigurationStore:
    """MCP JSON 配置:统一校验、原子写入;add/set_enabled/delete 供命令与导入共用。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_PATH

    def load(self) -> MCPSettings:
        if not self.path.is_file():
            return MCPSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return MCPSettings.model_validate(_normalize_legacy(raw))
        except (OSError, json.JSONDecodeError, ValueError):
            return MCPSettings()  # 配置损坏不阻断启动(降级取向;状态里可见 failed)

    def _write(self, settings: MCPSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.path)  # 原子替换
        finally:
            if temp.exists():
                temp.unlink()

    def add(self, server: MCPServerConfig) -> MCPSettings:
        """添加 server;重名拒绝(标准语义)。"""
        current = self.load()
        if any(s.name == server.name for s in current.servers):
            raise ValueError(f"MCP Server '{server.name}' 已存在")
        updated = MCPSettings(servers=(*current.servers, server))
        self._write(updated)
        return updated

    def set_enabled(self, name: str, enabled: bool) -> MCPServerConfig:
        found = next((s for s in self.load().servers if s.name == name), None)
        if found is None:
            raise KeyError(f"MCP Server '{name}' 不存在")
        updated_server = found.model_copy(update={"enabled": enabled})
        self._write(MCPSettings(servers=tuple(
            updated_server if s.name == name else s for s in self.load().servers)))
        return updated_server

    def delete(self, name: str) -> None:
        current = self.load()
        if not any(s.name == name for s in current.servers):
            raise KeyError(f"MCP Server '{name}' 不存在")
        self._write(MCPSettings(servers=tuple(s for s in current.servers if s.name != name)))


# ── 工具适配(deferred 注册;call_timeout 走配置) ──────────────────

class McpTool(Tool):
    """一个 MCP server 工具 → 一个 otter 工具(deferred,mcp__ 命名空间)。"""

    def __init__(self, server_name: str, registered_name: str, remote_name: str,
                 description: str, schema: dict, session, call_timeout: float = 30.0) -> None:
        self.name = registered_name
        self.description = f"[mcp:{server_name}] {description}"
        self.parameters = schema or {"type": "object", "properties": {}}
        self.deferred = True
        self._session = session
        self._remote_name = remote_name
        self._timeout = call_timeout

    async def run(self, args: dict[str, Any]) -> str:
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._remote_name, arguments=args),
                timeout=self._timeout)  # v0.5:超时走 server 配置(此前硬编码 60s)
            texts = [c.text for c in (result.content or []) if hasattr(c, "text")]
            body = "\n".join(texts) or "(空响应)"
            # 兼容两代 SDK 属性名(isError 驼峰旧版 / is_error 新版;真机暴露)
            is_error = getattr(result, "is_error", None)
            if is_error is None:
                is_error = getattr(result, "isError", False)
            if is_error:
                return f"[mcp 错误] {body[:2000]}"
            return body[:8000]
        except asyncio.TimeoutError:
            return f"[mcp 错误] 工具执行超过 {self._timeout:.0f}s"
        except Exception as exc:
            return f"[mcp 错误] {type(exc).__name__}: {exc}"


class McpStatusTool(Tool):
    """mcp_status:只读快照,不启动 server( 语义)。"""

    name = "mcp_status"
    description = "查看 MCP 服务器配置与连接状态快照(只读,不启动任何 server)。"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, manager: "MCPClientManager | None" = None) -> None:
        self._manager = manager

    async def run(self, args: dict[str, Any]) -> str:
        if self._manager is None:
            return json.dumps({"servers": []}, ensure_ascii=False)
        return json.dumps(
            [s.model_dump(mode="json") for s in self._manager.statuses()], ensure_ascii=False)


# ── 生命周期管理( ────────────────────────

class _ServerHandle:
    """一个 server 的守护任务句柄:ready/stop 事件 + 注册名单 + 错误。"""

    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.registered: list[str] = []
        self.error: str | None = None


class MCPClientManager:
    """隔离管理多个 MCP Server;失败回滚已注册工具,单点失败不拖累整体。

    生命周期修正(2026-09-24 真机暴露):官方 SDK 的 stdio_client 是带 anyio
    task group 的 async context manager,**不能手动 __aenter__ 挂在主流程**——
    否则主循环收尾时孤儿 task 的取消风暴会打断一切后续 await(实测 store.close
    被 CancelledError 击穿)。改为每个 server 一个后台守护任务,async with 的
    完整生命周期留在任务内,manager 经 ready/stop 事件与之通信。"""

    def __init__(self, configs: list[MCPServerConfig]) -> None:
        names = [c.name for c in configs]
        if len(names) != len(set(names)):
            raise ValueError("MCP Server 名称不能重复")
        self._configs = list(configs)
        self._handles: dict[str, _ServerHandle] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._states = {c.name: MCPServerStatus(name=c.name, state=MCPServerState.STOPPED)
                        for c in self._configs}

    async def start(self, registry) -> list[str]:
        """启动全部 enabled server 并注册工具;返回人读报告(单点失败跳过)。"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _hold(config: MCPServerConfig, handle: _ServerHandle) -> None:
            """守护任务:完整持有 stdio_client/ClientSession 的 async with 生命周期。"""
            try:
                params = StdioServerParameters(
                    command=config.command, args=list(config.args),
                    env={k: str(v) for k, v in config.env.items()} or None,
                    cwd=config.cwd)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(),
                                               timeout=config.startup_timeout_seconds)
                        listing = await session.list_tools()
                        seen: set[str] = set()
                        for t in listing.tools:
                            reg_name = mcp_tool_name(config.name, t.name)
                            if reg_name in seen:
                                raise ValueError(f"工具名规范化后冲突:{reg_name}")
                            seen.add(reg_name)
                            registry.register(McpTool(
                                config.name, reg_name, t.name, t.description or "",
                                getattr(t, "input_schema", None) or t.inputSchema,
                                session, config.call_timeout_seconds))
                            handle.registered.append(reg_name)
                        handle.ready.set()
                        await handle.stop.wait()  # 挂住直到 manager 要求收口
            except Exception as exc:
                handle.error = f"{type(exc).__name__}: {exc}"
                handle.ready.set()  # 唤醒等待方(失败路径)

        report: list[str] = []
        for config in self._configs:
            if not config.enabled:
                report.append(f"{config.name}(已禁用,跳过)")
                continue
            self._set(config.name, MCPServerState.STARTING)
            handle = _ServerHandle()
            self._handles[config.name] = handle
            self._tasks[config.name] = asyncio.create_task(_hold(config, handle))
            try:
                await asyncio.wait_for(handle.ready.wait(), timeout=config.startup_timeout_seconds + 5)
            except asyncio.TimeoutError:
                pass  # 超时按 failed 处理(handle.error 为空 → 标记超时)
            if handle.error is not None or not handle.registered:
                for n in reversed(handle.registered):  # 回滚半注册
                    registry.unregister(n)
                handle.registered.clear()
                err = handle.error or "启动超时或零工具"
                self._set(config.name, MCPServerState.FAILED, error=err)
                report.append(f"{config.name}(失败:{err[:80]},已回滚跳过)")
                continue
            self._set(config.name, MCPServerState.RUNNING, tool_names=tuple(handle.registered))
            report.append(f"{config.name}(运行中,{len(handle.registered)} 工具)")
        return report

    async def close(self, registry) -> None:
        """收口:置 stop 事件让守护任务自然走完 async with,再反注册工具。"""
        for name, handle in self._handles.items():
            handle.stop.set()
            task = self._tasks.get(name)
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except (asyncio.TimeoutError, Exception):
                    task.cancel()
            for tool_name in handle.registered:
                registry.unregister(tool_name)
            handle.registered.clear()
            self._set(name, MCPServerState.STOPPED)
        self._handles.clear()
        self._tasks.clear()

    def statuses(self) -> tuple[MCPServerStatus, ...]:
        return tuple(self._states[c.name] for c in self._configs)

    def _set(self, name: str, state: str, *, tool_names: tuple[str, ...] = (),
             error: str | None = None) -> None:
        self._states[name] = MCPServerStatus(
            name=name, state=state, tool_names=tool_names, error=error)


# ── 兼容入口(M4 以来 __main__ 的调用点) ────────────────────────────

_active_manager: MCPClientManager | None = None


async def connect_servers(registry) -> list[str]:
    """按 ~/.otter/mcp.json 连接并注册(v0.5:走 manager 状态机;坏配置静默空转)。"""
    global _active_manager
    settings = MCPConfigurationStore().load()
    if not settings.servers:
        return []
    _active_manager = MCPClientManager(list(settings.servers))
    report = await _active_manager.start(registry)
    # mcp_status 常驻注册(只读快照;重复注册幂等覆盖)
    registry.register(McpStatusTool(_active_manager))
    return report


async def close_servers(registry) -> None:
    """进程收口:反注册+关连接(__main__ 退出路径调用)。"""
    global _active_manager
    if _active_manager is not None:
        await _active_manager.close(registry)
        _active_manager = None
