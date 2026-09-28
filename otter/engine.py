"""库形态入口(2026-09-29 P1 库化):外部项目 `import otter` 把引擎当库用。

用法:
    import otter
    engine = await otter.build_engine(workspace="/path/to/project")
    code = await engine.run_task("帮我看下这个目录的结构并总结")
    await engine.aclose()

workspace 语义(显式传参,不再隐式依赖调用进程的 cwd):
- None(默认)= 当前进程目录,行为与 CLI 完全一致;
- 显式传入 = 引擎以该目录为工作区:装配期一次性切换进程工作目录,此后所有
  状态(.otter/otter.db、memory、artifacts、skills、tasks)与文件工具的
  相对路径基准全部落在该目录下。装配是同步阶段完成,不与事件循环并发,
  chdir 无竞态;调用方若不希望进程目录被改动,可在子进程中运行引擎。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from otter.config import Config
from otter.loop import MODE_NORMAL, AgentLoop, SummaryState
from otter.models import build_adapter
from otter.store import Store


@dataclass
class Engine:
    """装配完成的引擎实例:loop(执行核心)+ store(持久层)+ adapter(模型接入)。

    reconciled / mcp_report 记录装配期诊断(启动修正数、MCP 连接报告),
    打印责任在调用方——库形态不向 stdout 写任何内容。
    """

    loop: AgentLoop
    store: Store
    adapter: object
    config: Config
    summary_adapter: object | None = None
    reconciled: int = 0
    mcp_report: list[str] = field(default_factory=list)

    async def run_task(self, prompt: str, max_steps: int | None = None,
                       mode: str = MODE_NORMAL):
        """跑一条任务返回 AgentResult(复用 -p 单任务通道;统计打印与 REPL 一致)。"""
        from otter.repl import _dispatch  # 同包复用单任务装配(会话/run 记录+收尾统计)

        return await _dispatch(self.loop, self.store, [], SummaryState(),
                               prompt, max_steps or self.config.max_steps, mode)

    async def aclose(self) -> None:
        """释放底层连接(adapter HTTP 与 SQLite);引擎用完必须调用。"""
        await self.loop.adapter.close()
        if self.summary_adapter is not None and self.summary_adapter is not self.loop.adapter:
            await self.summary_adapter.close()
        await self.store.close()

    async def __aenter__(self) -> "Engine":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


async def build_engine(workspace: str | Path | None = None, config: Config | None = None,
                       yes: bool = False, connect_mcp: bool = True) -> Engine:
    """装配完整引擎(workspace/config 均可显式传入;详见模块 docstring)。

    yes=True 跳过审批门(CI/headless,等价 CLI --yes);connect_mcp=False
    跳过 ~/.otter/mcp.json 外部服务连接(隔离测试/嵌入式场景)。
    """
    if workspace is not None:
        # 2026-09-29 P1:显式工作区——装配期一次性切换,后续所有 cwd 基准落此
        os.chdir(Path(workspace).expanduser().resolve())
    from otter.workspaces import register_workspace

    register_workspace(Path.cwd())  # 最近工作区登记(失败静默,与 CLI 一致)

    cfg = config or Config.load()
    if not cfg.api_key:
        raise RuntimeError(
            "未配置 OTTER_API_KEY(复制 .env.example 为 .env 并填入,或传入带 key 的 Config)")

    store = Store()
    await store.open()
    reconciled = await store.reconcile_interrupted_runs()

    adapter = build_adapter(cfg.base_url, cfg.api_key, cfg.model,
                            provider=cfg.provider, max_tokens=cfg.max_tokens)
    summary_adapter = (
        adapter
        if not cfg.summary_model
        else build_adapter(cfg.base_url, cfg.api_key, cfg.summary_model,
                           provider=cfg.provider, max_tokens=cfg.max_tokens)
    )

    from otter.agents_md import load_instructions
    from otter.assembly import build_full, build_gate, make_sandbox
    from otter.context.summarizer import RollingSummarizer

    activated: set[str] = set()
    registry, memory_bundle = build_full(store, activated=activated, adapter=adapter)
    mcp_report: list[str] = []
    if connect_mcp:
        from otter.mcp_client import connect_servers

        mcp_report = await connect_servers(registry)

    loop = AgentLoop(
        adapter, registry, store,
        summarizer=RollingSummarizer(summary_adapter),
        context_budget=cfg.context_budget,
        trigger_ratio=cfg.trigger_ratio,
        keep_recent=cfg.keep_recent,
        git_auto=cfg.git_auto,
        instructions=load_instructions(),
        approval_gate=None if yes else build_gate(),
        sandbox=make_sandbox(cfg.sandbox),
        memory_bundle=memory_bundle,
        edit_format=os.environ.get("OTTER_EDIT_FORMAT", "auto"),
        activated_tools=activated,
        run_budget=cfg.run_budget,
        reflection_adapter=summary_adapter,
    )
    return Engine(loop=loop, store=store, adapter=adapter, config=cfg,
                  summary_adapter=summary_adapter, reconciled=reconciled,
                  mcp_report=mcp_report)
