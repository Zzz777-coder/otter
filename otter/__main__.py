"""CLI 入口:python -m otter(或安装后的 otter 命令)。

用法:
  otter                      进入交互 REPL
  otter -p "任务描述"         单任务模式(headless 雏形;结构化 JSON 输出是 M2)
  otter --max-steps 20       限制步数
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from otter import __version__
from otter.config import Config
from otter.loop import AgentLoop
from otter.models import build_adapter
from otter.repl import run_repl, run_single
from otter.store import Store
from otter.tools.builtin import builtin_registry


def main() -> int:
    parser = argparse.ArgumentParser(prog="otter", description="otter — 轻量 CLI 编码 Agent")
    parser.add_argument("-p", "--prompt", help="单任务模式:执行该任务后退出", default=None)
    parser.add_argument("--gui", action="store_true", help="启动桌面 GUI 薄壳(Tkinter,2026-09-19 新增)")
    parser.add_argument("--gui-probe", action="store_true", help="GUI 诊断模式:启动后跑 evaluate_js 真值探针并退出(2026-09-23)")
    parser.add_argument("--gui-e2e", action="store_true", help="GUI 端到端测试:真实任务→diff卡片→脚本点击→验磁盘,输出 PASS/FAIL(2026-09-23)")
    parser.add_argument("--gui-artifact-probe", action="store_true",
                        help="GUI 产物卡片探针:真实窗口注入事件流,DOM 断言卡片渲染(2026-09-23 #43)")
    parser.add_argument("--model", help="覆盖 OTTER_MODEL 配置", default=None)
    parser.add_argument("--max-steps", type=int, default=None, help="单次任务最大步数(默认取配置 30)")
    parser.add_argument("--plan", action="store_true", help="PLAN 只读模式(仅检索与分析,不写文件;M1 新增)")
    parser.add_argument("--resume", action="store_true", help="恢复最近一次中断的 Run(M2 新增)")
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text",
                        help="-p 模式输出格式:json=收尾一次性;stream-json=NDJSON 流式逐事件(2026-09-24 新增)")
    parser.add_argument("--yes", action="store_true",
                        help="跳过审批交互全部允许(供 CI/headless;请确认任务可信,M2 新增)")
    args = parser.parse_args()

    config = Config.load()
    if args.model:
        config.model = args.model
    max_steps = args.max_steps or config.max_steps

    if not config.api_key:
        print("错误:未配置 OTTER_API_KEY(复制 .env.example 为 .env 并填入,或设置环境变量)", file=sys.stderr)
        return 2

    # GUI 模式有自己的主循环(2026-09-19 M-GUI;M4 起为可选 extras,缺依赖时给出安装指引)
    if args.gui or args.gui_probe or args.gui_e2e or args.gui_artifact_probe:
        try:
            from otter.gui import launch_gui
        except ImportError as exc:
            print(f"GUI 依赖未安装({exc})。安装:pip install 'otter-agent[gui]'", file=sys.stderr)
            return 2
        # 2026-09-23 #43:新增 artifact_probe 通道(产物卡片偶发缺失定位与回归)
        return launch_gui(probe=args.gui_probe, e2e=args.gui_e2e, artifact_probe=args.gui_artifact_probe)

    async def _run() -> int:
        from otter.agents_md import load_instructions
        from otter.assembly import build_full, build_gate, make_sandbox
        from otter.context.summarizer import RollingSummarizer

        store = Store()
        await store.open()
        # M2 启动 reconciliation:遗留 running 的 Run 修正为 interrupted(Checkpoint 为事实源)
        fixed = await store.reconcile_interrupted_runs()
        if fixed:
            # 2026-09-24 修正:诊断提示改走 stderr——stream-json 模式下 stdout 必须是纯 NDJSON
            # (真机首跑即被这行污染,非法 JSON 行会打断 jq 逐行消费)
            print(f"[otter] 启动修正:{fixed} 个遗留 Run 已标记 interrupted(otter --resume 可恢复)",
                  file=sys.stderr)
        # 2026-09-24 起 adapter 经工厂装配:Anthropic 原生 / OpenAI 兼容二选一
        adapter = build_adapter(config.base_url, config.api_key, config.model,
                                provider=config.provider, max_tokens=config.max_tokens)
        summary_adapter = (
            adapter
            if not config.summary_model
            else build_adapter(config.base_url, config.api_key, config.summary_model,
                               provider=config.provider, max_tokens=config.max_tokens)
        )
        # M1→M4 装配:摘要/指令/git + 审批/沙箱/Evidence + 记忆/repo_map/deferred + 子代理 + MCP
        activated: set[str] = set()
        registry, memory_bundle = build_full(store, activated=activated, adapter=adapter)
        # M4 MCP:按 ~/.otter/mcp.json 连外部 server(无配置=静默跳过)
        from otter.mcp_client import connect_servers

        mcp_report = await connect_servers(registry)
        if mcp_report:
            # 2026-09-24 修正:同上,诊断提示走 stderr 保 stdout 纯 NDJSON
            print("[otter] MCP: " + "; ".join(mcp_report), file=sys.stderr)
        loop = AgentLoop(
            adapter, registry, store,
            summarizer=RollingSummarizer(summary_adapter),
            context_budget=config.context_budget,
            trigger_ratio=config.trigger_ratio,
            keep_recent=config.keep_recent,
            git_auto=config.git_auto,
            instructions=load_instructions(),
            approval_gate=None if args.yes else build_gate(),  # --yes:显式跳过审批(CI 用)
            sandbox=make_sandbox(config.sandbox),
            memory_bundle=memory_bundle,
            edit_format=os.environ.get("OTTER_EDIT_FORMAT", "auto"),
            activated_tools=activated,  # 修正:与 ToolSearchTool 同一对象(见 loop 注释)
            run_budget=config.run_budget,
        )
        try:
            if args.resume:
                from otter.repl import run_resume

                return await run_resume(loop, store, max_steps)
            if args.prompt is not None:
                mode = "plan" if args.plan else "normal"
                if args.output_format == "stream-json":
                    # 2026-09-24 headless stream-json:NDJSON 流式(常配 --yes 供 CI)
                    from otter.repl import run_single_stream

                    return await run_single_stream(loop, store, args.prompt, max_steps, mode)
                return await run_single(loop, store, args.prompt, max_steps, mode,
                                        json_output=args.output_format == "json")
            await run_repl(loop, store, max_steps)
            return 0
        finally:
            await adapter.close()
            if summary_adapter is not adapter:
                await summary_adapter.close()
            await store.close()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
